/**
 * K线训练 — 应用控制器 v2
 * ========================
 * 修复：指标超前、周期切换、K线动画、播放速度、主图指标控制
 */
const KlineApp = (function () {

  // ── 状态 ──
  let symbol = '', currentPeriod = '1H';
  let category = 'crypto';        // 品类：crypto | futures | stock（决定数据接口与 long-only gate）
  let longOnly = false;           // 仅做多品类（如A股）：禁用做空
  let rangeStart = null, rangeEnd = null;  // 固定区间（'YYYY-MM-DD'）；null=最近N根模式
  let blindMode = false, blindId = null;   // 盲选模式：品种隐藏、服务器分配、周期/区间锁定
  let blindSeed = null;                     // 揭晓后的盲选真实 seed（提交成绩时回填）
  let _summaryShown = false;                // 训练完成后汇总弹窗只自动弹一次
  let _lastSessionId = null;                // 最近一次提交成绩的 session_id
  // PK 对战模式：题面锁定（不可改品种/周期/区间），跑到最后一根自动提交
  let pkMode = false, pkMatchId = null, pkCode = null, pkBlind = false, pkSubmitted = false;
  // 自由组队：房间状态机驱动（大厅/就绪/进行中/暂停/观战），自动同步按全局时钟推进
  let pkTeam = false, pkAutoSync = false, pkSpectate = false, pkIsHost = false;
  let pkStatus = '', pkSpeedMs = 0, pkStartAt = 0, pkPauseMs = 0, pkPausedAt = 0;
  let _pkClockOffset = 0;                 // server_now - 本地 Date.now()（毫秒）
  let _pkPollTimer = null, _pkAutoTimer = null;   // 房间轮询 / 自动推进定时器
  let _pkChartReady = false;              // 组队进行中是否已取图并开始
  let _pkStarting = false;                // 防并发重复取图/启动
  let fullData = null;
  let visibleIndex = 0;
  let startIndex = 60;
  let isTraining = false;
  let autoPlayTimer = null;
  let autoSpeedMs = 2500;       // 1x = 2.5秒/根
  let tpslEnabled = false;
  // 已执行过交易引擎回调的最大bar下标（防止回退后重放重复触发止盈止损/权益记录）
  let processedBarIndex = -1;

  // 周期缓存 { period: fullDataResponse }
  const periodCache = {};

  // 图表实例
  let mainChart = null, candleSeries = null, volumeSeries = null;
  let macdChart = null, rsiChart = null, adxChart = null;
  let maSeries = {}, bollSeries = {}, sarSeries = null;
  let macdSeries = {}, rsiSeries = null, adxSeries = {};
  let equityChart = null, equityLine = null;

  // 主图指标开关（独立于顶部快捷按钮）
  const mainIndState = { ma: true, boll: true, sar: false };
  // 副图指标开关
  const subIndState = { macd: true, rsi: true, adx: false, atr: false };

  // 动画状态
  let _animFrame = null;
  let _isAnimating = false;

  // 时间轴同步守卫 — 防止 setData 过程中触发中间 sync 事件
  let _isUpdating = false;
  let _syncTimer = null;

  // 配色
  const C = {
    bg: '#0a0e17', grid: 'rgba(42,46,57,0.3)', text: '#787b86',
    up: '#26a69a', down: '#ef5350',
    ma5: '#ffa726', ma10: '#5c6bc0', ma20: '#e91e63', ma60: '#00bcd4',
    bollMid: 'rgba(255,167,38,0.5)', bollBand: 'rgba(92,107,192,0.5)',
    sar: '#ffab40', macdDif: '#2962ff', macdDea: '#ff6d00', rsi: '#7c4dff',
    adxMain: '#26a69a', adxPlus: '#4fc3f7', adxMinus: '#ef5350',
  };

  const CHART_OPTS = {
    layout: { background: { type: 'solid', color: C.bg }, textColor: C.text, fontSize: 11 },
    localization: { dateFormat: 'yyyy-MM-dd HH:mm' },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { width: 1, color: '#505460', style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: '#2962ff' },
      horzLine: { width: 1, color: '#505460', style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: '#2962ff' },
    },
    rightPriceScale: { borderColor: '#1e222d', scaleMargins: { top: 0.05, bottom: 0.22 } },
    timeScale: { borderColor: '#1e222d', timeVisible: true, secondsVisible: false, rightOffset: 8, barSpacing: 8, minBarSpacing: 2 },
    handleScroll: { vertTouchDrag: false },
  };

  const SUB_OPTS = {
    layout: { background: { type: 'solid', color: C.bg }, textColor: '#555', fontSize: 10 },
    localization: { dateFormat: 'yyyy-MM-dd HH:mm' },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#1e222d' },
    timeScale: { borderColor: '#1e222d', timeVisible: true, secondsVisible: false, visible: false },
    handleScroll: { vertTouchDrag: false },
  };

  function fmt(n, d = 4) {
    if (n == null || isNaN(n)) return '--';
    return Number(n).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: d });
  }

  /** 按时间过滤指标数据，确保不超过 visibleMaxTime */
  function _filterByTime(arr, visibleMaxTime) {
    if (!arr || arr.length === 0) return [];
    // 二分查找优化性能
    let lo = 0, hi = arr.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid].time <= visibleMaxTime) lo = mid + 1;
      else hi = mid;
    }
    return arr.slice(0, lo);
  }

  /** 获取当前可见K线的最大时间戳 */
  function _visibleMaxTime() {
    if (!fullData || !fullData.candles || visibleIndex < 1) return 0;
    return fullData.candles[visibleIndex - 1].time;
  }

  // ══════════════════════════════════════════════════════════
  //  初始化
  // ══════════════════════════════════════════════════════════

  function init(sym, cat, isLongOnly) {
    symbol = sym;
    category = cat || 'crypto';
    longOnly = !!isLongOnly;
    _initMainChart();
    _initSubCharts();
    _initEquityMini();
    _bindEvents();
    _bindKeyboard();
    TradeEngine.reset();
    // 仅做多品类（A股）：引擎层禁用做空 + 前端 gate 做空按钮
    TradeEngine.setConfig({ longOnly: longOnly });
    _applyLongOnlyGate();
    _syncTpSlUI();   // 止盈止损开关/参数UI与引擎配置对齐
    loadData(currentPeriod);
  }

  /** 仅做多品类：禁用做空按钮并改标注（A股 long-only gate） */
  function _applyLongOnlyGate() {
    const btn = document.getElementById('btn-short');
    if (!btn) return;
    if (longOnly) {
      btn.disabled = true;
      btn.textContent = '仅做多';
      btn.title = '该品种仅支持做多';
    } else {
      btn.disabled = false;
    }
  }

  function _initMainChart() {
    const wrap = document.getElementById('main-wrap');
    mainChart = LightweightCharts.createChart(wrap, { ...CHART_OPTS, width: wrap.clientWidth, height: wrap.clientHeight });
    candleSeries = mainChart.addCandlestickSeries({ upColor: C.up, downColor: C.down, borderDownColor: C.down, borderUpColor: C.up, wickDownColor: C.down, wickUpColor: C.up });
    volumeSeries = mainChart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
    mainChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    // 主图指标 overlay series — 一次性创建常驻，之后仅 setData（避免每根K线推进都销毁重建导致卡顿）
    const ovOpts = { lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    maSeries = {};
    for (const key of ['ma5', 'ma10', 'ma20', 'ma60']) {
      maSeries[key] = mainChart.addLineSeries({ ...ovOpts, color: C[key] });
    }
    bollSeries = {
      mid: mainChart.addLineSeries({ ...ovOpts, color: C.bollMid, lineStyle: 2 }),
      upper: mainChart.addLineSeries({ ...ovOpts, color: C.bollBand, lineStyle: 2 }),
      lower: mainChart.addLineSeries({ ...ovOpts, color: C.bollBand, lineStyle: 2 }),
    };
    sarSeries = mainChart.addLineSeries({ ...ovOpts, color: C.sar, lineStyle: 1 });
    mainChart.subscribeCrosshairMove(param => {
      const legend = document.getElementById('main-legend');
      if (!param.time || !param.seriesData || !param.seriesData.size) { legend.innerHTML = ''; return; }
      const c = param.seriesData.get(candleSeries);
      const v = param.seriesData.get(volumeSeries);
      if (!c) { legend.innerHTML = ''; return; }
      const clr = c.close >= c.open ? C.up : C.down;
      let html = `<span class="ohlc-label">O</span><span class="ohlc-val" style="color:${clr}">${fmt(c.open)}</span>` +
        `<span class="ohlc-label">H</span><span class="ohlc-val" style="color:${clr}">${fmt(c.high)}</span>` +
        `<span class="ohlc-label">L</span><span class="ohlc-val" style="color:${clr}">${fmt(c.low)}</span>` +
        `<span class="ohlc-label">C</span><span class="ohlc-val" style="color:${clr}">${fmt(c.close)}</span>` +
        `<span class="ohlc-label">V</span><span class="ohlc-val">${v ? fmt(v.value, 2) : '--'}</span>`;
      if (mainIndState.ma) {
        html += '<br>';
        for (const [key, s] of Object.entries(maSeries)) {
          const d = param.seriesData.get(s);
          if (d) html += `<span style="color:${C[key]};font-size:10px">${key.toUpperCase()}:${fmt(d.value)} </span>`;
        }
      }
      legend.innerHTML = html;
    });
    new ResizeObserver(entries => { for (const e of entries) mainChart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height }); }).observe(wrap);
  }

  function _initSubCharts() {
    const macdWrap = document.getElementById('macd-wrap');
    macdChart = LightweightCharts.createChart(macdWrap, { ...SUB_OPTS, width: macdWrap.clientWidth, height: macdWrap.clientHeight });
    macdSeries.dif = macdChart.addLineSeries({ color: C.macdDif, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    macdSeries.dea = macdChart.addLineSeries({ color: C.macdDea, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    macdSeries.hist = macdChart.addHistogramSeries({ priceFormat: { type: 'price' } });
    new ResizeObserver(entries => { for (const e of entries) macdChart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height }); }).observe(macdWrap);

    const rsiWrap = document.getElementById('rsi-wrap');
    rsiChart = LightweightCharts.createChart(rsiWrap, { ...SUB_OPTS, width: rsiWrap.clientWidth, height: rsiWrap.clientHeight });
    rsiSeries = rsiChart.addLineSeries({ color: C.rsi, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true });
    rsiChart._obLine = rsiChart.addLineSeries({ color: 'rgba(239,83,80,0.3)', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    rsiChart._osLine = rsiChart.addLineSeries({ color: 'rgba(38,166,154,0.3)', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    new ResizeObserver(entries => { for (const e of entries) rsiChart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height }); }).observe(rsiWrap);

    const adxWrap = document.getElementById('adx-wrap');
    adxChart = LightweightCharts.createChart(adxWrap, { ...SUB_OPTS, width: adxWrap.clientWidth, height: adxWrap.clientHeight });
    adxSeries.adx = adxChart.addLineSeries({ color: C.adxMain, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false });
    adxSeries.plusDi = adxChart.addLineSeries({ color: C.adxPlus, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    adxSeries.minusDi = adxChart.addLineSeries({ color: C.adxMinus, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    new ResizeObserver(entries => { for (const e of entries) adxChart.applyOptions({ width: e.contentRect.width, height: e.contentRect.height }); }).observe(adxWrap);
  }

  function _initEquityMini() {
    const wrap = document.getElementById('equity-wrap');
    if (!wrap) return;
    equityChart = LightweightCharts.createChart(wrap, {
      layout: { background: { type: 'solid', color: '#0f1320' }, textColor: '#555', fontSize: 9 },
      localization: { dateFormat: 'yyyy-MM-dd' },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      rightPriceScale: { borderColor: '#1e222d' },
      timeScale: { visible: false }, crosshair: { mode: 0 }, handleScroll: { vertTouchDrag: false },
      width: wrap.clientWidth, height: 60,
    });
    equityLine = equityChart.addAreaSeries({ topColor: 'rgba(41,98,255,0.3)', bottomColor: 'rgba(41,98,255,0.05)', lineColor: '#2962ff', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    new ResizeObserver(entries => { for (const e of entries) equityChart.applyOptions({ width: e.contentRect.width }); }).observe(wrap);
  }

  // ══════════════════════════════════════════════════════════
  //  数据加载（带缓存）
  // ══════════════════════════════════════════════════════════

  /** 构造品类感知 + 区间感知的取数 URL（固定区间 or 最近N根） */
  function _dataUrl(period) {
    let url = `/kline/api/kline-data/${category}/${symbol}?period=${period}`;
    if (rangeStart && rangeEnd) {
      // 传日期字符串（非本地时区 unix），与后端 naive 墙钟约定一致，无时区歧义
      url += `&start=${encodeURIComponent(rangeStart + ' 00:00:00')}` +
             `&end=${encodeURIComponent(rangeEnd + ' 23:59:59')}`;
    } else {
      url += `&full_data=true&target_bars=600`;
    }
    return url;
  }

  async function loadData(period, preserveTime) {
    if (blindMode) { showToast('盲选模式已锁定周期与区间', 'info'); return; }
    // 周期切换不再清空账户：若已有资金进度（交易记录或持仓），
    // 先快照旧状态 + 旧K线时间轴，加载新周期后按时间平滑迁移（保留资金进度）
    const carrying = TradeEngine.getTrades().length > 0 ||
      !!TradeEngine.getAccountState(_currentPrice() || 0).position;

    let snapshot = null, oldCandles = null, oldPrice = null;
    const wasTraining = isTraining;
    if (carrying && fullData && fullData.candles) {
      snapshot = TradeEngine.serialize();
      oldCandles = fullData.candles;
      oldPrice = _currentPrice();
      stopAutoPlay();   // 迁移期间暂停自动播放，避免下标错乱
    }

    currentPeriod = period;
    _resetLoadingOverlay();

    // 记录当前时间位置用于保持
    const prevTime = (preserveTime && fullData && visibleIndex > 0)
      ? fullData.candles[visibleIndex - 1].time : null;

    try {
      // 检查缓存
      if (periodCache[period]) {
        fullData = periodCache[period];
      } else {
        fullData = await _fetchJson(_dataUrl(period));
        periodCache[period] = fullData;
        _noMoreHistory = false;   // 新周期允许再次惰性加载
      }

      // 从默认起点开始显示；若需保持时间位置则映射到新周期的对应下标
      startIndex = 60;
      if (prevTime != null) {
        let idx = fullData.candles.findIndex(c => c.time >= prevTime);
        if (idx < 0) idx = fullData.candles.length - 1;
        visibleIndex = Math.max(startIndex, idx + 1);
      } else {
        visibleIndex = startIndex;
      }

      if (snapshot) {
        // ── 平滑迁移：余额/配置/交易/持仓按时间映射到新周期，资金进度不丢 ──
        const summary = TradeEngine.migrateToPeriod(
          snapshot, oldCandles, fullData.candles, visibleIndex, oldPrice);
        isTraining = wasTraining;                 // 保留训练态
        processedBarIndex = visibleIndex - 1;     // 防止迁移后重放重复触发
        TradeEngine.setBarIndex(visibleIndex - 1);
        _renderVisible();
        _updateAccountUI();
        _updateTradesUI();
        _updatePositionUI(_currentPrice() || 0);
        _updateEquityCurve();
        _notifyMigration(summary, period);
      } else {
        isTraining = false;
        processedBarIndex = -1;
        TradeEngine.reset();
        _renderVisible();
        _updateAccountUI();
        _updateTradesUI();
        _updateEquityCurve();
      }

      // 成功路径收起加载遮罩（失败时在 catch 提前 return，不会执行到此，遮罩由 _showLoadError 保留）
      const _lo = document.getElementById('main-loading');
      if (_lo) _lo.classList.add('hidden');
    } catch (err) {
      console.error('数据加载失败:', err);
      // 失败原因常驻显示（含后端给出的代理排查指引），不再只靠一闪而过的 toast
      _lastLoadError = err.message;
      _showLoadError(err.message);
      if (/代理|connect|timeout|连接|超时|unreachable/i.test(err.message)) {
        const d = await _probeNetwork();
        if (d && d.usable === false) {
          _showLoadError(err.message, _netHintHtml(d));
          return;
        }
      }
      return;
    }
  }

  /** 周期迁移结果提示：明确告知资金/持仓的保留与结算情况 */
  function _notifyMigration(summary, period) {
    if (!summary) { showToast(`已切换至 ${period}`, 'info'); return; }
    let msg = `已切换至 ${period}，账户资金已保留`;
    if (summary.keptLots > 0) msg += ` · 持仓 ${summary.keptLots} 笔已迁移`;
    if (summary.realizedLots > 0) msg += ` · ${summary.realizedLots} 笔无法映射已按原价结算落袋`;
    showToast(msg, summary.realizedLots > 0 ? 'info' : 'success');
  }

  /** 按固定区间加载（替代「最近N根」）；区间可复现，适合盲选/PK */
  function loadRange(startStr, endStr) {
    if (!startStr || !endStr) { showToast('请填写有效的起止日期', 'error'); return; }
    if (startStr >= endStr) { showToast('起始日期须早于结束日期', 'error'); return; }
    rangeStart = startStr; rangeEnd = endStr;
    for (const k in periodCache) delete periodCache[k];   // 区间变更 → 清缓存
    stopAutoPlay();
    isTraining = false;
    showToast(`按区间加载 ${startStr} ~ ${endStr}`, 'info');
    loadData(currentPeriod);
  }

  /** 恢复「最近N根」模式 */
  function clearRange() {
    if (rangeStart == null && rangeEnd == null) { showToast('当前已是最近N根模式', 'info'); return; }
    rangeStart = null; rangeEnd = null;
    for (const k in periodCache) delete periodCache[k];
    stopAutoPlay();
    isTraining = false;
    showToast('已恢复最近 600 根模式', 'info');
    loadData(currentPeriod);
  }

  /** 当前是否处于固定区间模式 */
  function isRangeMode() { return !!(rangeStart && rangeEnd); }

  // ══════════════════════════════════════════════════════════
  //  盲选模式（品种隐藏、服务器分配 + 去标识化 + 揭晓）
  // ══════════════════════════════════════════════════════════

  /** 盲选页初始化：建图/绑定后向服务器取题（不经 loadData/周期接口） */
  function initBlind() {
    blindMode = true;
    category = 'blind'; symbol = '';
    longOnly = false;                 // 盲选允许双向（不泄露「仅做多」→ 不暴露A股）
    _initMainChart(); _initSubCharts(); _initEquityMini();
    _bindEvents(); _bindKeyboard();
    TradeEngine.reset();
    TradeEngine.setConfig({ longOnly: false });
    _applyLongOnlyGate();
    _syncTpSlUI();
    startBlind();
  }

  /** 向服务器请求一道盲选题：注入去标识化K线并遮罩身份 */
  async function startBlind() {
    blindMode = true;
    _resetLoadingOverlay();
    const loading = document.getElementById('main-loading');
    if (loading) loading.classList.remove('hidden');
    try {
      const resp = await fetch('/kline/api/blind/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bar_count: 250 }),
      });
      const json = await resp.json();
      if (json.code !== 200 || !json.data || !json.data.chart ||
          !json.data.chart.candles || json.data.chart.candles.length === 0) {
        throw new Error(json.message || '盲选取题失败');
      }
      blindId = json.data.training_id;
      fullData = json.data.chart;
      currentPeriod = (fullData.meta && fullData.meta.period) || '1D';
      for (const k in periodCache) delete periodCache[k];
      periodCache[currentPeriod] = fullData;
      _noMoreHistory = true;          // 盲选固定窗口，不再惰性加载

      startIndex = 60;
      visibleIndex = Math.min(startIndex, fullData.candles.length);
      isTraining = false;
      _resetSummaryState();
      processedBarIndex = -1;
      TradeEngine.reset();
      TradeEngine.setConfig({ longOnly: false });
      _applyLongOnlyGate();
      _renderVisible();
      _updateAccountUI();
      _updateTradesUI();
      _updateEquityCurve();
      _applyBlindMask();
      if (loading) loading.classList.add('hidden');
      showToast('盲选题目已分配：预测走势后点「揭晓答案」', 'info');
    } catch (err) {
      console.error('盲选启动失败:', err);
      _lastLoadError = err.message;
      _showLoadError(err.message);
    }
  }

  /** 揭晓答案：恢复真实身份、解除遮罩、弹出答案面板 */
  async function revealBlind() {
    if (!blindId) { showToast('尚未开始盲选', 'error'); return; }
    try {
      const resp = await fetch('/kline/api/blind/' + blindId + '/reveal', { method: 'POST' });
      const json = await resp.json();
      if (json.code !== 200 || !json.data) throw new Error(json.message || '揭晓失败');
      const a = json.data;
      category = a.category; symbol = a.symbol; longOnly = !!a.long_only;
      blindSeed = a.seed || null;
      TradeEngine.setConfig({ longOnly: longOnly });
      _applyLongOnlyGate();
      _removeBlindMask(a);
      _showRevealPanel(a);
      showToast('答案揭晓：' + (a.category_label || '') + ' · ' + (a.name || ''), 'success');
    } catch (err) {
      console.error('揭晓失败:', err);
      showToast(err.message || '揭晓失败', 'error');
    }
  }

  /** 遮罩身份信息（品种/交易所）+ 显示盲选水印 */
  function _applyBlindMask() {
    const nameEl = document.getElementById('sym-name');
    const exchEl = document.getElementById('sym-exchange');
    if (nameEl) nameEl.textContent = '神秘品种';
    if (exchEl) exchEl.textContent = '盲选训练';
    const wm = document.getElementById('blind-watermark');
    if (wm) wm.style.display = '';
  }

  /** 解除遮罩：恢复真实品种名/交易所标签，隐藏水印 */
  function _removeBlindMask(a) {
    const nameEl = document.getElementById('sym-name');
    const exchEl = document.getElementById('sym-exchange');
    if (nameEl) nameEl.textContent = a.name || a.symbol || '';
    if (exchEl) exchEl.textContent = a.exchange || a.category_label || '';
    const wm = document.getElementById('blind-watermark');
    if (wm) wm.style.display = 'none';
  }

  /** 弹出答案面板 */
  function _showRevealPanel(a) {
    const body = document.getElementById('reveal-body');
    const modal = document.getElementById('reveal-modal');
    if (body) {
      const rows = [
        ['品类', a.category_label || a.category || '—'],
        ['品种', (a.name || '') + '（' + (a.symbol || '') + '）'],
        ['交易所/市场', a.exchange || '—'],
        ['周期', a.period || '—'],
        ['K线根数', a.bar_count != null ? a.bar_count : '—'],
      ];
      body.innerHTML = rows.map(function (r) {
        return '<div class="reveal-row"><span class="reveal-k">' + r[0] +
               '</span><span class="reveal-v">' + r[1] + '</span></div>';
      }).join('');
    }
    if (modal) modal.classList.add('show');
  }

  // ══════════════════════════════════════════════════════════
  //  回放控制
  // ══════════════════════════════════════════════════════════

  // ════════════════════════════════════════════════════════
  //  PK 同题对战（题面锁定 + 房主引擎参数 + 跑完自动提交）
  // ════════════════════════════════════════════════════════

  /** PK 训练页初始化：建图/绑定后按对战 id 取同题数据。
   *  opts.team → 自由组队（先轮询房间状态，playing 后才取图）；否则邀请码同题即时取图。*/
  async function initPk(matchId, isLongOnly, opts) {
    pkMode = true; pkMatchId = matchId; pkSubmitted = false;
    longOnly = !!isLongOnly;
    opts = opts || {};
    pkTeam = !!opts.team; pkAutoSync = !!opts.autoSync; pkSpectate = !!opts.spectate;
    _initMainChart(); _initSubCharts(); _initEquityMini();
    _bindEvents(); _bindKeyboard();
    TradeEngine.reset();
    _applyLongOnlyGate();
    _syncTpSlUI();
    if (pkSpectate) _applySpectateMode();
    if (pkTeam) {
      _pkTeamBoot();                 // 大厅/就绪/进行中 由轮询驱动
    } else {
      await loadPkChart();           // 邀请码同题：立即取图（行为不变）
    }
  }

  /** 向 /api/pk/<id>/chart 取锁定题目（未加入则先占位加入再取） */
  async function loadPkChart() {
    _resetLoadingOverlay();
    const loading = document.getElementById('main-loading');
    if (loading) loading.classList.remove('hidden');
    try {
      let resp = await fetch('/kline/api/pk/' + pkMatchId + '/chart');
      let json = await resp.json();
      if (json.code === 403 && !pkTeam) {         // 邀请码未加入 → 占位加入后重试（组队不走此路径）
        await fetch('/kline/api/pk/join', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ match_id: pkMatchId }),
        });
        resp = await fetch('/kline/api/pk/' + pkMatchId + '/chart');
        json = await resp.json();
      }
      if (json.code !== 200 || !json.data || !json.data.candles || !json.data.candles.length) {
        throw new Error(json.message || '对战取题失败');
      }
      const d = json.data, pk = d.pk || {}, meta = d.meta || {};
      fullData = d;
      category = pk.category || meta.category || 'crypto';
      symbol = (meta.code && meta.code !== '??????') ? meta.code : '';
      currentPeriod = meta.period || '1D';
      pkBlind = !!pk.blind; pkCode = pk.code;
      longOnly = !!pk.long_only;
      // 先套用房主引擎配置，再 reset 使 baseAmount 生效
      if (pk.engine_config) TradeEngine.setConfig(pk.engine_config);
      TradeEngine.reset();
      TradeEngine.setConfig({ longOnly: longOnly });
      _applyLongOnlyGate();
      for (const k in periodCache) delete periodCache[k];
      periodCache[currentPeriod] = fullData;
      _noMoreHistory = true;                   // 固定窗口，不惰性扩展
      startIndex = Math.min(60, Math.max(2, Math.floor(fullData.candles.length * 0.2)));
      visibleIndex = startIndex;
      isTraining = false; _resetSummaryState(); processedBarIndex = -1;
      _renderVisible(); _updateAccountUI(); _updateTradesUI(); _updateEquityCurve();
      _applyPkTitle(); _syncTpSlUI();
      if (loading) loading.classList.add('hidden');
      if (!pkTeam) {
        showToast('对战 #' + pkCode + ' 题目已锁定，点「开始训练」逐根推进，跑完自动提交', 'info');
      }
    } catch (err) {
      console.error('对战取题失败:', err);
      _lastLoadError = err.message; _showLoadError(err.message);
    }
  }

  /** PK 标题栏：用（可能已去标识的）meta 回填品种名/交易所 */
  function _applyPkTitle() {
    const nameEl = document.getElementById('sym-name');
    const exchEl = document.getElementById('sym-exchange');
    const meta = (fullData && fullData.meta) || {};
    if (nameEl) nameEl.textContent = meta.name || symbol || '神秘品种';
    if (exchEl) exchEl.textContent = meta.exchange || meta.category || '';
  }

  // ════════════════════════════════════════════════════════
  //  回放控制
  // ════════════════════════════════════════════════════════

  function startTraining() {
    if (!fullData || !fullData.candles || fullData.candles.length === 0) {
      showToast('请先加载数据', 'error'); return;
    }
    isTraining = true;
    _resetSummaryState();
    visibleIndex = startIndex;
    processedBarIndex = visibleIndex - 1;
    TradeEngine.setBarIndex(visibleIndex - 1);
    _renderVisible();
    _updateAccountUI();
    showToast('训练开始！使用空格键推进K线', 'info');
  }

  function resetTraining() {
    stopAutoPlay();
    isTraining = false;
    _resetSummaryState();
    visibleIndex = startIndex;
    processedBarIndex = -1;
    TradeEngine.reset();
    _syncTpSlUI();
    _renderVisible();
    _updateAccountUI();
    _updateTradesUI();
    _updateEquityCurve();
    showToast('训练已重置', 'info');
  }

  function randomStart() {
    if (!fullData || !fullData.candles || fullData.candles.length < 120) {
      showToast('数据不足，无法随机起点', 'error'); return;
    }
    stopAutoPlay();
    const minStart = 60;
    const maxStart = Math.floor(fullData.candles.length * 0.6);
    startIndex = minStart + Math.floor(Math.random() * (maxStart - minStart));
    visibleIndex = startIndex;
    TradeEngine.reset();
    // 随机起点后直接进入训练态，无需再点一次“开始训练”
    isTraining = true;
    _resetSummaryState();
    processedBarIndex = visibleIndex - 1;
    TradeEngine.setBarIndex(visibleIndex - 1);
    _renderVisible();
    _updateAccountUI();
    _updateTradesUI();
    _updateEquityCurve();
    showToast(`随机起点: 第 ${startIndex} 根K线，训练开始！`, 'info');
  }

  function nextBar() {
    if (!fullData) return;
    if (visibleIndex >= fullData.candles.length) {
      showToast('已到最后一根K线', 'info'); stopAutoPlay(); return;
    }
    visibleIndex++;

    const candle = fullData.candles[visibleIndex - 1];
    const barIdx = visibleIndex - 1;

    // 仅训练模式下驱动交易引擎；且只在首次推进到该K线时触发（回退后重放不重复）
    if (isTraining) {
      TradeEngine.setBarIndex(barIdx);
      if (barIdx > processedBarIndex) {
        processedBarIndex = barIdx;
        const result = TradeEngine.onBarUpdate(candle, barIdx, fullData.indicators);
        if (result) {
          result.forEach(r => {
            if (r.ok) showToast(r.msg, r.type === 'liquidation' ? 'error' : 'success');
            _addTradeAnnotation(r);
          });
        }
      }
    }

    _renderVisible();
    _updateAccountUI();
    _updatePositionUI(candle.close);

    // 训练推进到最后一根 → 弹出成绩汇总（每次训练仅自动弹一次）
    if (isTraining && visibleIndex >= fullData.candles.length) _onTrainingComplete();

    // K线内部价格动画
    _playSubBarAnimation(candle);
  }

  function prevBar() {
    if (visibleIndex <= startIndex) return;
    visibleIndex--;
    if (isTraining) TradeEngine.setBarIndex(visibleIndex - 1);
    _renderVisible();
    if (fullData && fullData.candles[visibleIndex - 1]) {
      _updatePositionUI(fullData.candles[visibleIndex - 1].close);
    }
  }

  function toggleAutoPlay() {
    if (autoPlayTimer) { stopAutoPlay(); return; }
    if (!isTraining) startTraining();
    autoPlayTimer = setInterval(() => {
      if (visibleIndex >= fullData.candles.length) { stopAutoPlay(); return; }
      nextBar();
    }, autoSpeedMs);
    document.getElementById('btn-auto').classList.add('running');
    document.getElementById('btn-auto').textContent = '⏸ 暂停';
  }

  function stopAutoPlay() {
    if (autoPlayTimer) { clearInterval(autoPlayTimer); autoPlayTimer = null; }
    document.getElementById('btn-auto').classList.remove('running');
    document.getElementById('btn-auto').textContent = '⏩ 自动';
  }

  // ══════════════════════════════════════════════════════════
  //  K线内部价格动画 — 客户端合成路径（open → 影线 → close）
  // ══════════════════════════════════════════════════════════

  function _playSubBarAnimation(candle) {
    if (_animFrame) { clearTimeout(_animFrame); _animFrame = null; }
    _isAnimating = false;

    // 高倍速自动播放时跳过动画，避免视觉噪声
    if (autoPlayTimer && autoSpeedMs < 500) return;

    // 合成价格路径：阳线 open→low→high→close，阴线 open→high→low→close，每段线性插值
    const up = candle.close >= candle.open;
    const anchors = up
      ? [candle.open, candle.low, candle.high, candle.close]
      : [candle.open, candle.high, candle.low, candle.close];
    const prices = [];
    const SUBDIV = 4;
    for (let i = 0; i < anchors.length - 1; i++) {
      for (let k = 1; k <= SUBDIV; k++) {
        prices.push(anchors[i] + (anchors[i + 1] - anchors[i]) * k / SUBDIV);
      }
    }
    if (prices.length < 2) return;

    _isAnimating = true;
    const duration = Math.min(autoSpeedMs * 0.6, 1000); // 动画占间隔的60%，最多1秒
    const stepTime = duration / prices.length;
    let step = 0;
    let runningHigh = candle.open, runningLow = candle.open;

    function tick() {
      if (!_isAnimating || step >= prices.length) {
        // 动画结束，确保显示最终OHLC
        candleSeries.update(candle);
        _isAnimating = false;
        _animFrame = null;
        return;
      }
      const p = prices[step];
      if (p > runningHigh) runningHigh = p;
      if (p < runningLow) runningLow = p;
      candleSeries.update({
        time: candle.time,
        open: candle.open,
        high: runningHigh,
        low: runningLow,
        close: p,
      });
      step++;
      _animFrame = setTimeout(tick, stepTime);
    }
    tick();
  }

  // ══════════════════════════════════════════════════════════
  //  交易操作
  // ══════════════════════════════════════════════════════════

  function doLong() {
    if (!isTraining) { showToast('请先开始训练', 'error'); return; }
    const price = _currentPrice();
    if (!price) return;
    const layers = _getSelectedLayers();
    const result = TradeEngine.openPosition('long', layers, price, visibleIndex - 1);
    if (result.ok) { showToast(result.msg, 'success'); _renderTradeMarkers(); }
    else showToast(result.msg, 'error');
    // 反手开仓会隐式平掉旧仓，需同步刷新交易列表与权益曲线
    _updateTradesUI(); _updateEquityCurve();
    _updateAccountUI(); _updatePositionUI(price);
  }

  function doShort() {
    if (longOnly) { showToast('该品种仅支持做多（A股不可做空）', 'error'); return; }
    if (!isTraining) { showToast('请先开始训练', 'error'); return; }
    const price = _currentPrice();
    if (!price) return;
    const layers = _getSelectedLayers();
    const result = TradeEngine.openPosition('short', layers, price, visibleIndex - 1);
    if (result.ok) { showToast(result.msg, 'success'); _renderTradeMarkers(); }
    else showToast(result.msg, 'error');
    _updateTradesUI(); _updateEquityCurve();
    _updateAccountUI(); _updatePositionUI(price);
  }

  function doClose() {
    if (!isTraining) { showToast('请先开始训练', 'error'); return; }
    const price = _currentPrice();
    if (!price) return;
    const result = TradeEngine.closePosition(price, visibleIndex - 1, '手动平仓');
    _afterClose(result, price);
  }

  /** 按比例部分减仓（如先平 50% 锁定利润，剩余继续持有） */
  function doClosePercent(pct) {
    if (!isTraining) { showToast('请先开始训练', 'error'); return; }
    const price = _currentPrice();
    if (!price) return;
    if (!TradeEngine.getAccountState(price).position) { showToast('无持仓', 'error'); return; }
    const result = TradeEngine.closePosition(price, visibleIndex - 1, '部分平仓', { percent: pct });
    _afterClose(result, price);
  }

  /** 平掉指定子仓位（分批建仓后的单笔独立平仓） */
  function doCloseLot(lotId) {
    if (!isTraining) { showToast('请先开始训练', 'error'); return; }
    const price = _currentPrice();
    if (!price) return;
    const result = TradeEngine.closePosition(price, visibleIndex - 1, '平子仓位', { lotId });
    _afterClose(result, price);
  }

  /** 平仓结果统一处理：提示 + 刷新标记/交易列表/权益/账户/持仓 */
  function _afterClose(result, price) {
    if (result.ok) {
      showToast(result.msg, (result.totalPnl || 0) >= 0 ? 'success' : 'error');
      _renderTradeMarkers(); _updateTradesUI(); _updateEquityCurve();
    } else {
      showToast(result.msg, 'error');
    }
    _updateAccountUI(); _updatePositionUI(price);
  }

  function toggleTpSl() {
    tpslEnabled = !tpslEnabled;
    TradeEngine.setConfig({ tpSlEnabled: tpslEnabled });
    _syncTpSlUI();
    // 持仓中开关止盈止损 → 刷新子仓位的止盈止损价显示
    const px = _currentPrice() || 0;
    if (TradeEngine.getAccountState(px).position) _updatePositionUI(px);
    showToast(`止盈止损: ${tpslEnabled ? '已开启' : '已关闭'}`, 'info');
  }

  /** 止盈止损参数实时同步到引擎（无需保存/刷新），并回写UI */
  function setTpSlConfig(partial) {
    TradeEngine.setConfig(partial);
    const c = TradeEngine.getConfig();
    tpslEnabled = c.tpSlEnabled;
    _syncTpSlUI();
    const px = _currentPrice() || 0;
    if (TradeEngine.getAccountState(px).position) _updatePositionUI(px);
  }

  /** 依据引擎配置回写止盈止损面板（开关、状态、模式、参数、可见性） */
  function _syncTpSlUI() {
    const c = TradeEngine.getConfig();
    tpslEnabled = c.tpSlEnabled;
    const sw = document.getElementById('sw-tpsl');
    if (sw) sw.classList.toggle('on', tpslEnabled);
    const st = document.getElementById('tpsl-status');
    if (st) { st.textContent = tpslEnabled ? '开' : '关'; st.className = 'tpsl-badge' + (tpslEnabled ? ' on' : ''); }
    const box = document.getElementById('tpsl-config');
    if (box) box.style.display = tpslEnabled ? '' : 'none';
    const modeSel = document.getElementById('live-tpsl-mode');
    if (modeSel) modeSel.value = c.tpSlMode || 'percent';
    const tp = document.getElementById('live-tp'); if (tp) tp.value = c.tpPercent;
    const sl = document.getElementById('live-sl'); if (sl) sl.value = c.slPercent;
    const psl = document.getElementById('live-protect-sl'); if (psl) psl.value = c.protectiveSlPercent || 0;
    _syncTpSlModeUI(c.tpSlMode || 'percent');
  }

  /** 按模式动态展示对应参数区（百分比 → TP/SL；MACD/SAR → 保护性止损） */
  function _syncTpSlModeUI(mode) {
    const pctBox = document.getElementById('live-tpsl-percent');
    const sigBox = document.getElementById('live-tpsl-signal');
    if (pctBox) pctBox.style.display = mode === 'percent' ? '' : 'none';
    if (sigBox) sigBox.style.display = (mode === 'macd' || mode === 'sar') ? '' : 'none';
    const hint = document.getElementById('live-tpsl-hint');
    if (hint) {
      hint.textContent = mode === 'macd' ? 'DIF 下穿 DEA 平多 / 上穿 DEA 平空；可叠加保护性止损'
        : mode === 'sar' ? '价格跌破 SAR 平多 / 突破 SAR 平空；可叠加保护性止损'
        : '按保证金收益率触发：止盈% / 止损%（已换算杠杆）';
    }
  }

  function _currentPrice() {
    if (!fullData || visibleIndex < 1) return null;
    return fullData.candles[visibleIndex - 1].close;
  }

  function _getSelectedLayers() {
    try { return getSelectedLayers(); } catch (e) { return 1; }
  }

  // ══════════════════════════════════════════════════════════
  //  图表渲染（修复指标超前 Fix 1）
  // ══════════════════════════════════════════════════════════

  function _renderVisible() {
    if (!fullData || !fullData.candles) return;
    const slice = fullData.candles.slice(0, visibleIndex);
    const volSlice = fullData.volumes.slice(0, visibleIndex);
    const ind = fullData.indicators || {};
    const maxTime = _visibleMaxTime();

    // 取消正在进行的动画
    if (_animFrame) { clearTimeout(_animFrame); _animFrame = null; }
    _isAnimating = false;

    // ── 设置守卫：阻止 setData 过程中触发的中间 sync 事件 ──
    _isUpdating = true;
    if (_syncTimer) { clearTimeout(_syncTimer); _syncTimer = null; }

    // ── 1. 批量设置所有数据 ──
    candleSeries.setData(slice);
    volumeSeries.setData(volSlice);

    // 主图指标 — 按时间截断绝不超前；series 常驻，关闭时置空数据
    const maData = ind.ma || {};
    for (const key of Object.keys(maSeries)) {
      maSeries[key].setData(mainIndState.ma ? _filterByTime(maData[key] || [], maxTime) : []);
    }
    const bollData = ind.boll || {};
    bollSeries.mid.setData(mainIndState.boll ? _filterByTime(bollData.mid || [], maxTime) : []);
    bollSeries.upper.setData(mainIndState.boll ? _filterByTime(bollData.upper || [], maxTime) : []);
    bollSeries.lower.setData(mainIndState.boll ? _filterByTime(bollData.lower || [], maxTime) : []);
    sarSeries.setData(mainIndState.sar ? _filterByTime(ind.sar || [], maxTime) : []);

    // 副图 MACD — 按时间截断
    if (subIndState.macd && ind.macd) {
      document.getElementById('macd-wrap').style.display = '';
      macdSeries.dif.setData(_filterByTime(ind.macd.dif, maxTime));
      macdSeries.dea.setData(_filterByTime(ind.macd.dea, maxTime));
      macdSeries.hist.setData(_filterByTime(ind.macd.histogram, maxTime));
    } else {
      document.getElementById('macd-wrap').style.display = 'none';
    }

    // 副图 RSI
    _renderSubRSI(ind.rsi, maxTime);

    // 副图 ADX — 按时间截断
    if (subIndState.adx && ind.adx) {
      document.getElementById('adx-wrap').style.display = '';
      adxSeries.adx.setData(_filterByTime(ind.adx, maxTime));
      adxSeries.plusDi.setData(_filterByTime(ind.plus_di, maxTime));
      adxSeries.minusDi.setData(_filterByTime(ind.minus_di, maxTime));
    } else {
      document.getElementById('adx-wrap').style.display = 'none';
    }

    // ATR 显示
    if (subIndState.atr && ind.atr) {
      const atrSlice = _filterByTime(ind.atr, maxTime);
      const lastAtr = atrSlice.length > 0 ? atrSlice[atrSlice.length - 1] : null;
      _updateAtrDisplay(lastAtr ? lastAtr.value : null);
    } else {
      _updateAtrDisplay(null);
    }

    // 交易标注
    _renderTradeMarkers();

    // ── 2. 主图 fitContent，然后统一同步所有副图 ──
    // 滚动到最新位置，显示最后~80根K线
    if (slice.length > 80) {
      mainChart.timeScale().scrollToRealTime();
    } else {
      mainChart.timeScale().fitContent();
    }
    _syncSubCharts();

    // ── 3. 延迟释放守卫，确保所有中间事件被忽略 ──
    _syncTimer = setTimeout(() => {
      _isUpdating = false;
      _syncTimer = null;
      // 最终再同步一次，确保副图与主图完全对齐
      _syncSubCharts();
    }, 0);

    // 进度条
    const total = fullData.candles.length;
    const pct = total > 0 ? (visibleIndex / total * 100) : 0;
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('progress-text').textContent = `${visibleIndex}/${total}`;

    // 头部信息
    const lastCandle = slice[slice.length - 1];
    if (lastCandle) {
      const el = document.getElementById('hdr-price');
      el.textContent = fmt(lastCandle.close);
      el.className = 'tb-price ' + (lastCandle.close >= lastCandle.open ? 'up' : 'down');
    }
    const meta = fullData.meta || {};
    // 仅当数据带交易所信息时覆盖（crypto=OKX）；期货/股票保留模板注入的品类标签
    const _exchEl = document.getElementById('sym-exchange');
    if (_exchEl && meta.exchange) _exchEl.textContent = meta.exchange;
    document.getElementById('bar-count').textContent = `显示 ${visibleIndex}/${total} 根K线 · ${meta.period || currentPeriod}`;

    // 同步主图指标按钮状态
    _syncMainIndButtons();
  }

  /** 统一同步副图时间轴到主图的可见逻辑范围 */
  function _syncSubCharts() {
    if (!mainChart) return;
    const range = mainChart.timeScale().getVisibleLogicalRange();
    if (!range) return;
    const macdWrap = document.getElementById('macd-wrap');
    const rsiWrap = document.getElementById('rsi-wrap');
    const adxWrap = document.getElementById('adx-wrap');
    if (macdWrap && macdWrap.style.display !== 'none' && macdChart) {
      macdChart.timeScale().setVisibleLogicalRange(range);
    }
    if (rsiWrap && rsiWrap.style.display !== 'none' && rsiChart) {
      rsiChart.timeScale().setVisibleLogicalRange(range);
    }
    if (adxWrap && adxWrap.style.display !== 'none' && adxChart) {
      adxChart.timeScale().setVisibleLogicalRange(range);
    }
  }

  function _renderSubRSI(rsiData, maxTime) {
    const wrap = document.getElementById('rsi-wrap');
    if (subIndState.rsi && rsiData && rsiData.length > 0) {
      wrap.style.display = '';
      const sliced = _filterByTime(rsiData, maxTime);
      rsiSeries.setData(sliced);
      if (sliced.length > 0) {
        const times = sliced.map(d => d.time);
        rsiChart._obLine.setData(times.map(t => ({ time: t, value: 70 })));
        rsiChart._osLine.setData(times.map(t => ({ time: t, value: 30 })));
      }
      // 不调用 fitContent — 由 _syncSubCharts 统一同步
    } else { wrap.style.display = 'none'; rsiSeries.setData([]); }
  }

  function _clearOverlays() {
    // series 常驻不销毁，仅清空数据（供重置时调用）
    for (const s of Object.values(maSeries)) s.setData([]);
    for (const s of Object.values(bollSeries)) s.setData([]);
    if (sarSeries) sarSeries.setData([]);
  }

  function _updateAtrDisplay(val) {
    let el = document.getElementById('atr-display');
    if (!el) {
      el = document.createElement('span'); el.id = 'atr-display';
      el.style.cssText = 'font-size:11px;color:#787b86;';
      document.getElementById('toolbar').appendChild(el);
    }
    el.textContent = val != null ? `ATR: ${fmt(val)}` : '';
  }

  // ── 主图指标按钮同步 ──
  function _syncMainIndButtons() {
    document.querySelectorAll('.main-ind-btn').forEach(btn => {
      const key = btn.dataset.ind;
      if (mainIndState.hasOwnProperty(key)) btn.classList.toggle('active', mainIndState[key]);
    });
    document.querySelectorAll('.ind-btn').forEach(btn => {
      const key = btn.dataset.ind;
      if (subIndState.hasOwnProperty(key)) btn.classList.toggle('active', subIndState[key]);
      if (mainIndState.hasOwnProperty(key)) btn.classList.toggle('active', mainIndState[key]);
    });
  }

  // ── 交易标注 ──
  function _renderTradeMarkers() {
    const allTrades = TradeEngine.getTrades();
    if (allTrades.length === 0) { candleSeries.setMarkers([]); return; }
    const markers = [];
    const candles = fullData.candles;
    const maxTime = _visibleMaxTime();
    allTrades.forEach(t => {
      if (t.entryBarIndex >= 0 && t.entryBarIndex < candles.length) {
        const time = candles[t.entryBarIndex].time;
        if (time <= maxTime) {
          markers.push({
            time, position: t.direction === 'long' ? 'belowBar' : 'aboveBar',
            color: t.direction === 'long' ? '#26a69a' : '#ef5350',
            shape: t.direction === 'long' ? 'arrowUp' : 'arrowDown',
            text: `${t.direction === 'long' ? '多' : '空'} ${t.layers}层`,
          });
        }
      }
      if (t.exitBarIndex >= 0 && t.exitBarIndex < candles.length) {
        const time = candles[t.exitBarIndex].time;
        if (time <= maxTime) {
          markers.push({
            time, position: t.direction === 'long' ? 'aboveBar' : 'belowBar',
            color: t.pnl >= 0 ? '#26a69a' : '#ef5350',
            shape: t.direction === 'long' ? 'arrowDown' : 'arrowUp',
            text: `平 ${t.pnl >= 0 ? '+' : ''}${t.pnl}`,
          });
        }
      }
    });
    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
  }

  function _addTradeAnnotation(r) { _renderTradeMarkers(); _updateTradesUI(); _updateEquityCurve(); }

  // ══════════════════════════════════════════════════════════
  //  UI 更新
  // ══════════════════════════════════════════════════════════

  function _updateAccountUI() {
    const price = _currentPrice() || 0;
    const st = TradeEngine.getAccountState(price);
    const set = (id, val, cls) => {
      const el = document.getElementById(id);
      if (el) { el.textContent = val; if (cls) el.className = 'acct-val ' + cls; }
    };
    set('a-balance', fmt(st.balance));
    const eqCls = st.equity >= TradeEngine.getConfig().baseAmount ? 'up' : 'down';
    set('a-equity', fmt(st.equity), eqCls);
    set('a-available', fmt(st.availableBalance));
    const urCls = st.unrealizedPnl >= 0 ? 'up' : 'down';
    set('a-unrealized', `${st.unrealizedPnl >= 0 ? '+' : ''}${fmt(st.unrealizedPnl)} (${st.unrealizedPnlPct}%)`, st.unrealizedPnl !== 0 ? urCls : '');
    set('a-margin', fmt(st.marginUsed));
    set('a-margin-ratio', fmt(st.marginRatio, 1) + '%');
  }

  function _updatePositionUI(price) {
    const st = TradeEngine.getAccountState(price);
    const section = document.getElementById('pos-section');
    if (!st.position) { section.style.display = 'none'; return; }
    section.style.display = '';
    const p = st.position;
    const dirText = p.direction === 'long' ? '🟢 做多' : '🔴 做空';
    const pnlCls = st.unrealizedPnl >= 0 ? 'up' : 'down';
    const maxLayers = TradeEngine.getConfig().maxLayers;
    let html = `
      <div class="pos-row"><span class="pos-label">方向</span><span class="pos-val">${dirText}</span></div>
      <div class="pos-row"><span class="pos-label">持仓均价</span><span class="pos-val">${fmt(p.entryPrice)}</span></div>
      <div class="pos-row"><span class="pos-label">现价</span><span class="pos-val">${fmt(price)}</span></div>
      <div class="pos-row"><span class="pos-label">总数量</span><span class="pos-val">${fmt(p.size, 6)}</span></div>
      <div class="pos-row"><span class="pos-label">总价值</span><span class="pos-val">${fmt(p.value, 2)}</span></div>
      <div class="pos-row"><span class="pos-label">杠杆</span><span class="pos-val">${fmt(p.leverage, 2)}x</span></div>
      <div class="pos-row"><span class="pos-label">层数</span><span class="pos-val">${fmt(p.layers, 1)}/${maxLayers}</span></div>
      <div class="pos-row"><span class="pos-label">强平价</span><span class="pos-val" style="color:#ef5350">${fmt(st.liquidationPrice)}</span></div>
      <div class="pos-row"><span class="pos-label">未实现盈亏</span><span class="pos-val ${pnlCls}">${st.unrealizedPnl >= 0 ? '+' : ''}${fmt(st.unrealizedPnl)} (${st.unrealizedPnlPct}%)</span></div>
      <div class="pos-row"><span class="pos-label">持仓K线</span><span class="pos-val">${p.barsHeld}根 · ${p.lotCount}笔子仓</span></div>
      <div class="partial-close-row">
        <button class="pc-btn" onclick="KlineApp.doClosePercent(25)">平25%</button>
        <button class="pc-btn" onclick="KlineApp.doClosePercent(50)">平50%</button>
        <button class="pc-btn" onclick="KlineApp.doClosePercent(75)">平75%</button>
        <button class="pc-btn all" onclick="KlineApp.doClose()">全平</button>
      </div>
      <div class="lots-title">子仓位明细 · 独立止盈止损</div>`;
    p.lots.forEach(lot => {
      const lc = lot.unrealizedPnl >= 0 ? 'up' : 'down';
      const tpTxt = lot.tpPrice != null ? fmt(lot.tpPrice) : '—';
      const slTxt = lot.slPrice != null ? fmt(lot.slPrice) : '—';
      html += `
        <div class="lot-item">
          <div class="lot-row1">
            <span class="lot-tag">#${lot.id} · ${fmt(lot.layers, 1)}层 · ${fmt(lot.size, 4)} @ ${fmt(lot.entryPrice)}</span>
            <span class="lot-pnl ${lc}">${lot.unrealizedPnl >= 0 ? '+' : ''}${fmt(lot.unrealizedPnl)}</span>
            <button class="lot-close" onclick="KlineApp.doCloseLot(${lot.id})">平</button>
          </div>
          <div class="lot-tpsl">止盈 ${tpTxt} · 止损 ${slTxt} · 盈亏率 ${lot.unrealizedPnlPct}% · ${lot.barsHeld}根</div>
        </div>`;
    });
    document.getElementById('pos-info').innerHTML = html;
  }

  function _updateTradesUI() {
    const tbody = document.getElementById('trade-tbody');
    if (!tbody) return;
    const trades = TradeEngine.getTrades();
    tbody.innerHTML = trades.map(t => `
      <tr>
        <td>${t.id}</td>
        <td class="${t.direction === 'long' ? 'up' : 'down'}">${t.direction === 'long' ? '多' : '空'}</td>
        <td>${fmt(t.entryPrice)}</td>
        <td>${fmt(t.exitPrice)}</td>
        <td class="${t.pnl >= 0 ? 'up' : 'down'}">${t.pnl >= 0 ? '+' : ''}${fmt(t.pnl)}</td>
        <td class="${t.pnlPct >= 0 ? 'up' : 'down'}">${t.pnlPct}%</td>
        <td style="font-size:9px;color:#555">${t.reason}</td>
      </tr>
    `).join('');
  }

  function refreshStats() {
    const s = TradeEngine.getStatistics();
    const grid = document.getElementById('stats-grid');
    if (!grid) return;
    const row = (label, val, cls) => `<span class="stat-label">${label}</span><span class="stat-val${cls ? ' ' + cls : ''}">${val}</span>`;
    grid.innerHTML =
      row('总交易', s.totalTrades) +
      row('盈利/亏损', `${s.winTrades} / ${s.lossTrades}`) +
      row('胜率', fmt(s.winRate, 1) + '%', s.winRate >= 50 ? ' up' : ' down') +
      row('总盈亏', (s.totalPnl >= 0 ? '+' : '') + fmt(s.totalPnl), s.totalPnl >= 0 ? ' up' : ' down') +
      row('平均盈亏', fmt(s.avgPnl)) +
      row('平均盈利', '+' + fmt(s.avgWinPnl), ' up') +
      row('平均亏损', fmt(s.avgLossPnl), ' down') +
      row('盈亏比', fmt(s.profitLossRatio)) +
      row('利润因子', fmt(s.profitFactor)) +
      row('最大回撤', fmt(s.maxDrawdown) + ' (' + fmt(s.maxDrawdownPct, 1) + '%)', ' down') +
      row('最大连胜', s.maxConsecutiveWins) +
      row('最大连亏', s.maxConsecutiveLosses) +
      row('平均持仓', fmt(s.avgBarsHeld, 1) + '根K线') +
      row('Sharpe', fmt(s.sharpe, 2)) +
      row('最终余额', fmt(s.finalBalance), s.returnPct >= 0 ? ' up' : ' down') +
      row('收益率', (s.returnPct >= 0 ? '+' : '') + fmt(s.returnPct, 2) + '%', s.returnPct >= 0 ? ' up' : ' down');
    _updateEquityCurve();
  }

  function _updateEquityCurve() {
    if (!equityLine || !equityChart) return;
    const curve = TradeEngine.getEquityCurve();
    if (curve.length < 2) { equityLine.setData([]); return; }
    const candles = fullData?.candles;
    if (!candles) return;
    const data = curve
      .filter(p => p.index >= 0 && p.index < candles.length)
      .map(p => ({ time: candles[p.index].time, value: p.equity }));
    const seen = new Set();
    const unique = data.filter(d => { if (seen.has(d.time)) return false; seen.add(d.time); return true; });
    equityLine.setData(unique);
    equityChart.timeScale().fitContent();
  }

  // ══════════════════════════════════════════════════════════
  //  事件绑定
  // ══════════════════════════════════════════════════════════

  function _bindEvents() {
    // 周期按钮 — 保持时间线位置 (Fix 2)
    document.getElementById('period-btns').addEventListener('click', e => {
      const btn = e.target.closest('.period-btn');
      if (!btn) return;
      document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadData(btn.dataset.period, true);  // preserveTime = true
    });

    // 顶部快捷指标按钮 — 同时切换主图/副图
    document.getElementById('ind-btns').addEventListener('click', e => {
      const btn = e.target.closest('.ind-btn');
      if (!btn) return;
      const key = btn.dataset.ind;
      if (mainIndState.hasOwnProperty(key)) {
        mainIndState[key] = !mainIndState[key];
      }
      if (subIndState.hasOwnProperty(key)) {
        subIndState[key] = !subIndState[key];
      }
      btn.classList.toggle('active');
      if (fullData) _renderVisible();
    });

    // 主图指标控制按钮 (Fix 5)
    const mainIndPanel = document.getElementById('main-ind-btns');
    if (mainIndPanel) {
      mainIndPanel.addEventListener('click', e => {
        const btn = e.target.closest('.main-ind-btn');
        if (!btn) return;
        const key = btn.dataset.ind;
        if (mainIndState.hasOwnProperty(key)) {
          mainIndState[key] = !mainIndState[key];
          btn.classList.toggle('active', mainIndState[key]);
          // 同步顶部快捷按钮
          document.querySelectorAll(`.ind-btn[data-ind="${key}"]`).forEach(b => b.classList.toggle('active', mainIndState[key]));
          if (fullData) _renderVisible();
        }
      });
    }

    // 速度滑块 (Fix 4)
    const speedSlider = document.getElementById('speed-slider');
    if (speedSlider) {
      speedSlider.addEventListener('input', e => {
        const val = parseFloat(e.target.value);
        // 滑块范围 0.5~5秒，对应速度显示
        autoSpeedMs = val * 1000;
        const label = document.getElementById('speed-label');
        if (label) label.textContent = `${val.toFixed(1)}s/根`;
        if (autoPlayTimer) { stopAutoPlay(); toggleAutoPlay(); }
      });
    }

    // 速度预设按钮
    document.getElementById('speed-btns').addEventListener('click', e => {
      const btn = e.target.closest('.speed-btn');
      if (!btn) return;
      document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      autoSpeedMs = parseFloat(btn.dataset.ms);
      // 同步滑块
      if (speedSlider) speedSlider.value = autoSpeedMs / 1000;
      const label = document.getElementById('speed-label');
      if (label) label.textContent = `${(autoSpeedMs/1000).toFixed(1)}s/根`;
      if (autoPlayTimer) { stopAutoPlay(); toggleAutoPlay(); }
    });

    // 时间轴同步 + 惰性加载 — 主图驱动副图
    if (mainChart) {
      mainChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (_isUpdating || !range) return;
        _syncSubCharts();
        _checkLoadMore(range);
      });
    }
  }

  function _bindKeyboard() {
    document.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
      switch (e.key) {
        case ' ': e.preventDefault(); nextBar(); break;
        case 'ArrowRight': e.preventDefault(); nextBar(); break;
        case 'ArrowLeft': e.preventDefault(); prevBar(); break;
        case 'b': case 'B': doLong(); break;
        case 's': case 'S': doShort(); break;
        case 'p': case 'P': doClose(); break;
      }
    });
  }

  // ── 设置（基础金额/手续费/杠杆/分仓）──
  // 止盈止损已改为独立面板实时生效，不在此处处理，避免被保存动作覆盖
  function applySettings(cfg) {
    TradeEngine.setConfig(cfg);
    _syncTpSlUI();
    if (!isTraining) resetTraining();
  }

  // ── Toast ──
  function showToast(msg, type) {
    const el = document.createElement('div');
    el.className = 'toast ' + (type || 'info');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  // ══════════════════════════════════════════════════════════
  //  网络请求 — 统一超时保护 + 失败可视化
  // ══════════════════════════════════════════════════════════

  /**
   * 带超时与错误语义的 JSON 请求。
   * 后端错误会返回非 2xx 状态码，但 body 仍是 JSON，故不依据 resp.ok 判定，
   * 统一读 body.code，把后端给出的可操作提示（如代理未开启）透传到界面。
   */
  async function _fetchJson(url, timeoutMs = 100000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const resp = await fetch(url, { signal: ctrl.signal });
      let json = null;
      try { json = await resp.json(); } catch (e) { /* 非 JSON 响应 */ }
      if (!json) throw new Error(`服务响应异常（HTTP ${resp.status}）`);
      if (json.code !== 200 || !json.data) {
        throw new Error(json.message || `接口返回错误（code ${json.code}）`);
      }
      return json.data;
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('请求超时，请检查网络/代理后重试');
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function _resetLoadingOverlay() {
    const loading = document.getElementById('main-loading');
    loading.classList.remove('hidden');
    loading.innerHTML = '<span class="loading-spin">加载中…</span>';
  }

  /** 加载失败时常驻展示原因 + 重试按钮（toast 2.6 秒就消失，不足以看清故障） */
  function _showLoadError(msg, diag) {
    const loading = document.getElementById('main-loading');
    loading.classList.remove('hidden');
    loading.innerHTML = `
      <div class="load-error">
        <div class="le-title">K线数据加载失败</div>
        <div class="le-msg">${(msg || '未知错误').replace(/</g, '&lt;')}</div>
        ${diag || ''}
        <button class="le-btn" id="btn-retry-load">重试</button>
      </div>`;
    const btn = document.getElementById('btn-retry-load');
    if (btn) btn.onclick = () => { _resetLoadingOverlay(); loadData(currentPeriod); };
  }

  /** 自检后端到 OKX 的连通性；返回健康信息，不可用时给出代理指引 */
  async function _probeNetwork() {
    const el = document.getElementById('net-status');
    try {
      const resp = await fetch('/kline/api/health');
      const json = await resp.json();
      const d = json.data;
      if (!d) return null;
      if (el) {
        const used = (d.checks || []).find(c => c.ok);
        if (used) {
          el.textContent = `OKX ${used.transport === 'proxy' ? '代理' : '直连'} · ${used.latency_ms}ms`;
          el.className = 'net-ok';
        } else {
          el.textContent = 'OKX 不可达';
          el.className = 'net-bad';
        }
      }
      return d;
    } catch (e) {
      if (el) { el.textContent = '后端不可达'; el.className = 'net-bad'; }
      return null;
    }
  }

  /** 网络不可用时拼出排查指引 */
  function _netHintHtml(d) {
    const proxy = d && d.proxies ? d.proxies : '';
    const lines = proxy
      ? [`当前系统代理：${proxy}，但该通道不可用`,
         '请检查代理客户端是否运行、节点是否能连通境外站点']
      : ['未检测到可用代理，而本机无法直连 OKX',
         '请开启系统代理（Clash / V2Ray 等）后再重试'];
    return `<div class="le-hint">${lines.map(l => '· ' + l).join('<br>')}</div>`;
  }


  // ══════════════════════════════════════════════════════════
  //  惰性加载 — 滚动到左侧边界时加载更多历史数据
  // ══════════════════════════════════════════════════════════

  let _loadingMore = false;

  async function _checkLoadMore(range) {
    if (blindMode || (rangeStart && rangeEnd)) return;   // 盲选/固定区间：不向左惰性扩展
    if (_loadingMore || !range || !fullData || !fullData.candles || fullData.candles.length < 50) return;
    
    const logicalLength = range.to - range.from;
    const threshold = logicalLength * 0.3;
    
    if (range.from <= threshold) {
      const oldestCandle = fullData.candles[0];
      if (!oldestCandle) return;
      
      _loadingMore = true;
      const loading = document.getElementById('main-loading');
      loading.classList.remove('hidden');
      
      try {
        const resp = await fetch(
          '/kline/api/kline-more/' + category + '/' + symbol +
          '?period=' + currentPeriod +
          '&before_ts=' + oldestCandle.time +
          '&target_bars=300'
        );
        const json = await resp.json();
        if (json.code !== 200 || !json.data || !json.data.candles || json.data.candles.length === 0) {
          _loadingMore = false;
          loading.classList.add('hidden');
          return;
        }

        const moreData = json.data;
        const firstTime = fullData.candles[0].time;

        // 只保留严格早于现有最早K线的数据，防止时间重叠导致 setData 抛错
        let cut = moreData.candles.length;
        while (cut > 0 && moreData.candles[cut - 1].time >= firstTime) cut--;
        if (cut <= 0) {
          _loadingMore = false;
          loading.classList.add('hidden');
          return;
        }
        const offset = cut;

        fullData.candles = moreData.candles.slice(0, cut).concat(fullData.candles);
        fullData.volumes = moreData.volumes.slice(0, cut).concat(fullData.volumes);
        fullData.indicators = _mergeIndicators(moreData.indicators, fullData.indicators, firstTime);
        if (fullData.meta) fullData.meta.bar_count = fullData.candles.length;
        periodCache[currentPeriod] = fullData;

        // 头部插入后所有下标整体后移，同步偏移回放与交易引擎的锚点
        visibleIndex += offset;
        startIndex += offset;
        if (processedBarIndex >= 0) processedBarIndex += offset;
        TradeEngine.shiftIndices(offset);

        _renderVisible();

        // 保持用户当前浏览的可视区域（逻辑区间同步右移 offset）
        mainChart.timeScale().setVisibleLogicalRange({
          from: range.from + offset,
          to: range.to + offset,
        });
        _syncSubCharts();

        _updateAccountUI();
      } catch (err) {
        console.error('惰性加载失败:', err);
      } finally {
        _loadingMore = false;
        loading.classList.add('hidden');
      }
    }
  }

  /** 合并新旧指标：oldInd（更早的历史段）拼在前，并按 boundaryTime 去重防止时间重叠 */
  function _mergeIndicators(oldInd, newInd, boundaryTime) {
    const cutOld = arr => (arr || []).filter(d => d.time < boundaryTime);
    const merged = {};
    for (const key of Object.keys(newInd || {})) {
      if (Array.isArray(newInd[key])) {
        merged[key] = cutOld(oldInd[key]).concat(newInd[key]);
      } else if (typeof newInd[key] === 'object' && newInd[key] !== null) {
        merged[key] = {};
        for (const subKey of Object.keys(newInd[key])) {
          const oldArr = oldInd[key] ? oldInd[key][subKey] || [] : [];
          merged[key][subKey] = cutOld(oldArr).concat(newInd[key][subKey]);
        }
      } else {
        merged[key] = newInd[key];
      }
    }
    return merged;
  }
  // ══════════════════════════════════════════
  //  成绩汇总与提交（复用 TradeEngine.getStatistics()）
  // ══════════════════════════════════════════

  /** 重置汇总状态（新训练开始时）：允许再次自动弹窗 + 复位提交按钮 */
  function _resetSummaryState() {
    _summaryShown = false;
    _lastSessionId = null;
    const btn = document.getElementById('btn-submit-score');
    if (btn) { btn.textContent = '💾 提交成绩'; btn.disabled = false; }
  }

  /** 训练推进到最后一根：自动弹出汇总（仅一次） */
  function _onTrainingComplete() {
    if (_summaryShown) return;
    _summaryShown = true;
    stopAutoPlay();
    if (pkMode) {
      if (pkSpectate) return;               // 观战：跑到最后一根不提交不结算
      showSummary();
      showToast('已跑到最后一根K线，正在自动提交对战成绩…', 'info');
      pkSubmitScore();
      return;
    }
    showSummary();
    showToast('训练完成！已生成成绩汇总', 'success');
  }

  /** 打开成绩汇总弹窗并填充统计（复用 TradeEngine.getStatistics） */
  function showSummary() {
    if (!fullData) { showToast('请先加载数据', 'error'); return; }
    const s = TradeEngine.getStatistics();
    const meta = fullData.meta || {};
    const modeLabel = blindMode ? '盲选' : (isRangeMode() ? '固定时段' : '自由训练');
    const who = (blindMode && !symbol) ? '神秘品种' : (symbol || meta.code || '');
    _sumSet('summary-head', modeLabel + ' · ' + who + ' · ' + currentPeriod +
            ' · ' + fullData.candles.length + ' 根');
    _sumSet('sum-final-balance', fmt(s.finalBalance, 2));
    _sumSetColored('sum-return-pct', (s.returnPct >= 0 ? '+' : '') + Number(s.returnPct).toFixed(2) + '%', s.returnPct);
    _sumSet('sum-win-rate', Number(s.winRate).toFixed(1) + '%');
    _sumSetColored('sum-max-dd', Number(s.maxDrawdownPct).toFixed(2) + '%', -s.maxDrawdownPct);
    _sumSet('sum-plr', s.profitLossRatio === Infinity ? '∞' : Number(s.profitLossRatio).toFixed(2));
    _sumSet('sum-trades', String(s.totalTrades));
    _sumSet('sum-sharpe', Number(s.sharpe).toFixed(2));
    _sumSetColored('sum-pnl', (s.totalPnl >= 0 ? '+' : '') + fmt(s.totalPnl, 2), s.totalPnl);
    const modal = document.getElementById('summary-modal');
    if (modal) modal.classList.add('show');
    const btn = document.getElementById('btn-submit-score');
    if (btn && !_lastSessionId) { btn.textContent = '💾 提交成绩'; btn.disabled = false; }
  }

  function _sumSet(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }
  function _sumSetColored(id, text, val) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'sum-val ' + (val > 0 ? 'up' : (val < 0 ? 'down' : ''));
  }

  /** 关闭汇总弹窗 */
  function hideSummary() {
    const modal = document.getElementById('summary-modal');
    if (modal) modal.classList.remove('show');
  }

  /** 提交成绩到后端持久化（匿名可提交；P3 鉴权后归属账号） */
  async function submitScore() {
    if (!fullData || !fullData.candles || fullData.candles.length === 0) {
      showToast('无数据可提交', 'error'); return;
    }
    if (pkMode) return pkSubmitScore();
    const s = TradeEngine.getStatistics();
    const meta = fullData.meta || {};
    const mode = blindMode ? 'blind' : (isRangeMode() ? 'range' : 'free');
    const payload = {
      category: category || 'unknown',
      symbol: symbol || (blindMode ? '??????' : ''),
      period: currentPeriod,
      mode: mode,
      seed: meta.seed || blindSeed || null,
      start_ts: fullData.candles[0].time,
      bar_count: fullData.candles.length,
      stats: s,
      trades: TradeEngine.getTrades(),
      equity: TradeEngine.getEquityCurve(),
    };
    if (blindMode && blindId) payload.blind_id = blindId;
    try {
      const resp = await fetch('/kline/api/session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const json = await resp.json();
      if (json.code !== 200 || !json.data) throw new Error(json.message || '提交失败');
      _lastSessionId = json.data.session_id;
      const btn = document.getElementById('btn-submit-score');
      if (btn) { btn.textContent = '✓ 已提交 #' + _lastSessionId; btn.disabled = true; }
      showToast('成绩已提交（#' + _lastSessionId + '）', 'success');
    } catch (err) {
      console.error('提交成绩失败:', err);
      showToast(err.message || '提交失败', 'error');
    }
  }

  /** PK：提交对战成绩到 /api/pk/<id>/submit（跑完最后一根自动调用） */
  async function pkSubmitScore() {
    if (pkSpectate) return;                 // 观战不提交
    if (pkSubmitted) { showToast('本场成绩已提交', 'info'); return; }
    const payload = {
      stats: TradeEngine.getStatistics(),
      trades: TradeEngine.getTrades(),
      equity: TradeEngine.getEquityCurve(),
    };
    const btn = document.getElementById('btn-submit-score');
    if (btn) { btn.disabled = true; btn.textContent = '提交中…'; }
    try {
      const resp = await fetch('/kline/api/pk/' + pkMatchId + '/submit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const json = await resp.json();
      if (json.code !== 200 || !json.data) throw new Error(json.message || '提交失败');
      pkSubmitted = true;
      const d = json.data, mr = d.my_result || {};
      _lastSessionId = mr.session_id || _lastSessionId;
      if (btn) { btn.textContent = '✓ 已提交 · 名次 #' + (mr.rank || '-'); btn.disabled = true; }
      showToast('对战成绩已提交！名次 #' + (mr.rank || '-') + '/' + d.participants +
                (d.status === 'finished' ? ' · 对战已结束' : ''), 'success');
    } catch (err) {
      console.error('对战提交失败:', err);
      if (btn) { btn.disabled = false; btn.textContent = '💾 提交成绩'; }
      showToast(err.message || '提交失败', 'error');
    }
  }

  // ════════════════════════════════════════════════════════
  //  自由组队：房间状态机（大厅/就绪/进行中/暂停/结束/观战）
  //  轮询 GET /api/pk/<id> 驱动；自动同步按全局时钟逐根推进
  // ════════════════════════════════════════════════════════

  function _esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>]/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  }

  async function _pkGet(url) {
    const r = await fetch(url);
    const j = await r.json();
    if (j.code !== 200 || !j.data) throw new Error(j.message || ('接口错误 ' + j.code));
    return j.data;
  }
  async function _pkPost(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const j = await r.json();
    if (j.code !== 200 || !j.data) throw new Error(j.message || ('接口错误 ' + j.code));
    return j.data;
  }

  function _pkOverlay(html, kind) {
    const el = document.getElementById('pk-team');
    if (!el) return;
    if (!html) { el.className = 'pk-team hidden'; el.innerHTML = ''; return; }
    el.className = 'pk-team ' + (kind || 'modal');
    el.innerHTML = html;
  }

  function _pkApplyClock(v) {
    _pkClockOffset = (v.server_now || 0) - Date.now();
    pkSpeedMs = v.speed_ms || 0; pkStartAt = v.start_at || 0;
    pkPauseMs = v.pause_ms || 0; pkPausedAt = v.paused_at || 0;
    pkIsHost = !!v.is_host;
  }

  async function _pkTeamBoot() {
    await _pkTeamTick();
    if (_pkPollTimer) clearInterval(_pkPollTimer);
    _pkPollTimer = setInterval(_pkTeamTick, 1500);
  }

  let _pkTicking = false;
  async function _pkTeamTick() {
    if (_pkTicking) return; _pkTicking = true;
    try {
      let v;
      try { v = await _pkGet('/kline/api/pk/' + pkMatchId); }
      catch (e) { _pkOverlay('<div class="pk-card"><div class="pk-title">⚠️ ' + _esc(e.message) + '</div></div>', 'modal'); return; }
      _pkApplyClock(v);
      pkStatus = v.status;
      if (v.status === 'playing' || v.status === 'paused') {
        if (!_pkChartReady && !_pkStarting) await _pkTeamStartPlay(v);
        _pkOverlay(_pkHudHtml(v), 'hud');
      } else if (v.status === 'finished') {
        _pkOnFinished(v);
      } else {
        _pkOverlay(_pkLobbyHtml(v), 'modal');
      }
    } finally { _pkTicking = false; }
  }

  /** 进行中/暂停：取图后按模式启动（手动→自由逐根；自动→全局时钟定速推进） */
  async function _pkTeamStartPlay(v) {
    _pkStarting = true;
    try {
      await loadPkChart();
      _pkChartReady = true;
      if (pkAutoSync) {
        // 训练态就位，但由 _pkAutoTick 按钟表驱动，不依赖 startTraining 的提示
        isTraining = true; _resetSummaryState();
        visibleIndex = startIndex; processedBarIndex = visibleIndex - 1;
        TradeEngine.setBarIndex(visibleIndex - 1);
        _renderVisible(); _updateAccountUI(); _updateTradesUI(); _updateEquityCurve();
        _startPkAutoLoop();
      } else {
        startTraining();
      }
    } finally { _pkStarting = false; }
  }

  function _startPkAutoLoop() {
    if (_pkAutoTimer) clearInterval(_pkAutoTimer);
    _pkAutoTimer = setInterval(_pkAutoTick, 150);
  }

  /** 自动同步推进：由全局时钟推算应到达的根号，落后则逐根补齐（过时不候） */
  function _pkAutoTick() {
    if (!fullData || !pkAutoSync) return;
    const nowEff = pkPausedAt ? pkPausedAt : (Date.now() + _pkClockOffset);
    let elapsed = nowEff - pkStartAt - pkPauseMs;
    if (elapsed < 0) elapsed = 0;
    let target = startIndex + Math.floor(elapsed / (pkSpeedMs || 1));
    if (target > fullData.candles.length) target = fullData.candles.length;
    let guard = 0;
    while (visibleIndex < target && visibleIndex < fullData.candles.length && guard < 400) {
      nextBar(); guard++;
    }
  }

  function _pkOnFinished(v) {
    if (_pkAutoTimer) { clearInterval(_pkAutoTimer); _pkAutoTimer = null; }
    if (_pkPollTimer) { clearInterval(_pkPollTimer); _pkPollTimer = null; }
    if (_pkChartReady && !pkSpectate) { try { hideSummary(); } catch (e) {} }
    _pkOverlay(_pkFinishedHtml(v), 'modal');
  }

  function _pkStateChip(mm) {
    const s = { invited: '待接受', accepted: '已加入', ready: '已就绪', declined: '已拒绝' }[mm.state] || mm.state;
    return s + (mm.submitted ? ' ✓' : '');
  }

  function _pkTopicLine(v) {
    if (v.blind) return '🎲 盲选（对战结束后揭示品种与区间）· ' + (v.category_label || v.category || '');
    return (v.symbol_name || v.symbol || '') + ' · ' + (v.period || '') + ' · ' + (v.bar_count || '?') + ' 根';
  }

  function _pkEstDur(v) {
    if (!v.auto_sync || !v.speed_ms || !v.bar_count) return '';
    const sec = Math.round(v.speed_ms * v.bar_count / 1000);
    const m = Math.floor(sec / 60), s = sec % 60;
    return '预计时长 ≈ ' + (m ? m + '分' : '') + s + '秒 · 每根 ' + v.speed_ms + 'ms';
  }

  function _pkMembersHtml(v) {
    if (!v.members || !v.members.length) return '<div class="pk-hint">暂无成员</div>';
    return '<div class="pk-members">' + v.members.map(mm =>
      `<span class="pk-mem ${mm.online ? 'on' : ''} ${mm.role === 'host' ? 'host' : ''}">` +
      `<i class="pk-dot"></i>${mm.role === 'host' ? '👑 ' : ''}${_esc(mm.display)}` +
      `${mm.is_me ? '（我）' : ''}<em>${_pkStateChip(mm)}</em></span>`).join('') + '</div>';
  }

  function _pkLobbyHtml(v) {
    const mode = v.auto_sync ? '⏱ 自动同步推进' : '🖱 手动逐根';
    let actions = '';
    if (v.my_player_state === 'invited') {
      actions = `<button class="pk-cta" onclick="KlineApp.pkTeamAct('accept')">✅ 接受邀请</button>` +
        ` <button class="pk-btn" onclick="KlineApp.pkTeamAct('decline')">拒绝</button>`;
    } else if (v.status === 'lobby') {
      if (pkSpectate) {
        actions = `<div class="pk-hint">👀 观战：等待房间开始后进入</div>`;
      } else if (v.is_host) {
        const ok = (v.participants || 0) >= 2;
        actions = `<button class="pk-cta" ${ok ? '' : 'disabled'} onclick="KlineApp.pkTeamAct('start')">▶ 开始比赛</button>` +
          `<div class="pk-hint">${ok ? '全员将点「确定」后正式开始' : ('等待至少 2 名成员接受邀请（已加入 ' + v.participants + '）')}</div>`;
      } else {
        actions = `<div class="pk-hint">已加入，等待房主开始…（成员 ${v.participants}）</div>`;
      }
    } else if (v.status === 'ready') {
      const readyN = (v.members || []).filter(x => x.state === 'ready').length;
      const total = v.participants || 0;
      if (v.my_player_state === 'ready') actions = `<div class="pk-hint">已就绪，等待其他成员…</div>`;
      else if (pkSpectate) actions = `<div class="pk-hint">👀 观战：等待全员就绪…</div>`;
      else actions = `<button class="pk-cta" onclick="KlineApp.pkTeamAct('confirm')">✅ 确定，准备开始</button>`;
      actions += `<div class="pk-hint">就绪 ${readyN}/${total}</div>`;
    }
    return `<div class="pk-card">` +
      `<div class="pk-title">⚔️ 自由组队 · 房间 ${_esc(v.code)}</div>` +
      `<div class="pk-sub">${_pkTopicLine(v)} · ${mode}</div>` +
      (v.auto_sync ? `<div class="pk-sub">${_pkEstDur(v)}</div>` : '') +
      _pkMembersHtml(v) + `<div class="pk-actions">${actions}</div></div>`;
  }

  function _pkProgText(v) {
    if (v.auto_sync && v.speed_ms && v.start_at) {
      const now = v.paused_at ? v.paused_at : (Date.now() + _pkClockOffset);
      const el = Math.max(0, now - v.start_at - (v.pause_ms || 0));
      const bar = Math.min(v.bar_count || 0, Math.floor(el / v.speed_ms));
      return '进度 ' + bar + '/' + (v.bar_count || '?') + ' 根';
    }
    return '逐根推进中';
  }

  function _pkHudHtml(v) {
    const done = (v.members || []).filter(x => x.submitted).length;
    let host = '';
    if (v.is_host && !pkSpectate) {
      if (v.auto_sync) {
        host += v.status === 'paused'
          ? ` <button class="pk-mini" onclick="KlineApp.pkTeamAct('resume')">▶ 继续</button>`
          : ` <button class="pk-mini" onclick="KlineApp.pkTeamAct('pause')">⏸ 暂停</button>`;
      }
      host += ` <button class="pk-mini danger" onclick="KlineApp.pkTeamAct('end')">⏹ 结束</button>`;
    }
    const tag = v.status === 'paused' ? '⏸ 已暂停' : (pkSpectate ? '👀 观战中' : '⚔️ 进行中');
    return `<div class="pk-hud ${v.status === 'paused' ? 'paused' : ''}">` +
      `<span>${tag} · ${_pkProgText(v)} · 已完成 ${done}/${v.participants}</span>${host}</div>`;
  }

  function _pkFinishedHtml(v) {
    const my = v.my_result || {};
    let mine = '';
    if (my && my.completed) {
      const rp = (my.return_pct >= 0 ? '+' : '') + Number(my.return_pct || 0).toFixed(2) + '%';
      mine = `<div class="pk-sub">我的成绩：名次 #${my.rank || '-'} / ${v.participants} · 收益率 ${rp}</div>`;
    } else if (!pkSpectate) {
      mine = `<div class="pk-sub">你未完成本局（缺赛）</div>`;
    }
    return `<div class="pk-card">` +
      `<div class="pk-title">🏁 对战已结束 · ${_esc(v.code)}</div>` +
      `<div class="pk-sub">${_pkTopicLine(v)}</div>` + mine +
      `<div class="pk-actions">` +
      `<a class="pk-cta" style="text-decoration:none" href="/kline/pk/board/${v.id}">🏆 查看榜单</a> ` +
      `<a class="pk-btn" style="text-decoration:none" href="/kline/pk/compare/${v.id}">📈 权益对比</a></div></div>`;
  }

  /** 大厅/进行中各操作统一入口 */
  async function pkTeamAct(action) {
    const ep = { accept: 'accept', decline: 'decline', start: 'start', confirm: 'confirm', pause: 'pause', resume: 'resume', end: 'end' }[action];
    if (!ep) return;
    if (action === 'end' && !confirm('确定提前结束本场对战？未提交者将记为缺赛。')) return;
    if (action === 'decline' && !confirm('拒绝该邀请？')) return;
    try {
      await _pkPost('/kline/api/pk/' + pkMatchId + '/' + ep, {});
    } catch (e) { showToast(e.message || '操作失败', 'error'); }
    await _pkTeamTick();
  }

  /** 观战：隐藏交易与回放操作，仅看行情自动推进 */
  function _applySpectateMode() {
    ['btn-long', 'btn-short', 'btn-close-pos'].forEach(id => { const b = document.getElementById(id); if (b) b.disabled = true; });
    ['btn-start-train', 'btn-reset', 'btn-summary', 'btn-auto', 'btn-next', 'btn-prev', 'btn-random-start'].forEach(id => { const b = document.getElementById(id); if (b) b.style.display = 'none'; });
  }

  return {
    init, initBlind, initPk, startBlind, revealBlind,
    pkTeamAct,
    loadData, loadRange, clearRange, isRangeMode,
    startTraining, resetTraining, randomStart,
    nextBar, prevBar, toggleAutoPlay,
    doLong, doShort, doClose, doClosePercent, doCloseLot, toggleTpSl, setTpSlConfig,
    applySettings, refreshStats, showToast,
    showSummary, hideSummary, submitScore,
  };
})();


