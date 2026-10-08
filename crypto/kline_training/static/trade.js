/**
 * K线训练 — 模拟交易引擎 v2
 * ========================
 * 客户端交易模拟，核心能力：
 *   1) 多子仓位(lot)模型：同方向可分批建仓，每笔子仓位独立记录入场价/数量/保证金；
 *   2) 分批减仓/平仓：支持按子仓位、按百分比部分平仓，剩余仓位继续持有；
 *   3) 逐仓独立止盈止损：每笔子仓位独立计算并触发 TP/SL，账户总盈亏为各子仓位之和；
 *   4) 跨周期状态迁移：周期切换时保留资金进度，按时间把持仓/交易映射到新周期。
 */
const TradeEngine = (function () {

  // ── 默认配置 ──
  const DEFAULT_CFG = {
    baseAmount: 100,
    commissionRate: 0.0005,       // 0.05%
    leverage: 10,
    positionSizing: true,         // 分仓模式（按层数比例分配保证金）
    maxLayers: 10,
    maintenanceMarginRate: 0.005, // 维持保证金率 0.5%
    tpSlEnabled: false,           // 止盈止损全局开关
    tpSlMode: 'percent',          // percent | macd | sar
    tpPercent: 5,                 // 止盈百分比（按保证金收益率）
    slPercent: 3,                 // 止损百分比（按保证金收益率）
    protectiveSlPercent: 0,       // MACD/SAR 模式下的保护性止损（0=关闭）
    longOnly: false,              // 仅做多（A股等）：引擎层拒绝开空，纵深防御
  };

  // ── 状态 ──
  let cfg = { ...DEFAULT_CFG };
  let balance = cfg.baseAmount;
  let position = null;            // { direction, lots:[lot,...] } 单一方向、可含多个子仓位
  let trades = [];                // 已平仓交易记录（每次分批平仓生成一条）
  let equityCurve = [];           // [{index, equity}, ...]
  let peakEquity = cfg.baseAmount;
  let currentBarIndex = 0;
  let tradeIdCounter = 0;
  let lotIdCounter = 0;

  // ══════════════════════════════════════════════════════════
  //  生命周期 / 配置
  // ══════════════════════════════════════════════════════════

  /** 重置账户状态。注意：不重置 cfg（配置与状态分离，
   *  避免用户保存的设置被 reset 抹掉）；如需同时改配置传入 newCfg */
  function reset(newCfg) {
    if (newCfg) cfg = { ...cfg, ...newCfg };
    balance = cfg.baseAmount;
    position = null;
    trades = [];
    equityCurve = [];
    peakEquity = cfg.baseAmount;
    currentBarIndex = 0;
    tradeIdCounter = 0;
    lotIdCounter = 0;
  }

  /** 更新配置。止盈止损等参数变化会实时作用到已有子仓位（无需刷新/重开仓） */
  function setConfig(newCfg) {
    cfg = { ...cfg, ...newCfg };
    _recomputeLotTpSl();
  }

  function getConfig() { return { ...cfg }; }

  function getBarIndex() { return currentBarIndex; }
  function setBarIndex(idx) { currentBarIndex = idx; }

  /** 惰性加载向头部插入历史K线后，整体偏移所有下标锚点 */
  function shiftIndices(offset) {
    if (!offset) return;
    currentBarIndex += offset;
    if (position) position.lots.forEach(l => { l.entryBarIndex += offset; });
    trades.forEach(t => {
      if (t.entryBarIndex >= 0) t.entryBarIndex += offset;
      if (t.exitBarIndex >= 0) t.exitBarIndex += offset;
    });
    equityCurve.forEach(p => { p.index += offset; });
  }

  // ── 序列化 / 还原（周期切换保留进度用） ──
  function serialize() {
    return JSON.parse(JSON.stringify({
      cfg, balance, position, trades, equityCurve,
      peakEquity, currentBarIndex, tradeIdCounter, lotIdCounter,
    }));
  }

  function restore(state) {
    if (!state) return;
    cfg = { ...cfg, ...(state.cfg || {}) };
    balance = state.balance != null ? state.balance : cfg.baseAmount;
    position = state.position || null;
    trades = Array.isArray(state.trades) ? state.trades : [];
    equityCurve = Array.isArray(state.equityCurve) ? state.equityCurve : [];
    peakEquity = state.peakEquity != null ? state.peakEquity : balance;
    currentBarIndex = state.currentBarIndex || 0;
    tradeIdCounter = state.tradeIdCounter || 0;
    lotIdCounter = state.lotIdCounter || 0;
  }

  /**
   * 周期切换：把旧周期的账户状态迁移到新周期，尽量不损失资金进度。
   *   - balance / cfg / 计数器：始终保留；
   *   - 已平仓 trades：按时间重映射 entry/exit 下标，无法映射置 -1（仅影响图表标记，记录仍用于统计）；
   *   - 持仓 position：逐个子仓位按入场时间重映射；无法映射的子仓位以 oldPrice 结算落袋并生成记录；
   *   - equityCurve：按时间重映射，落在新周期之外或超过当前可见位置的点丢弃。
   * @returns {{keptLots:number, realizedLots:number, mappedTrades:number, droppedMarkers:number, hasPosition:boolean}}
   */
  function migrateToPeriod(snapshot, oldCandles, newCandles, newVisibleIndex, oldPrice) {
    restore(snapshot);

    const oldTimeAt = idx => (oldCandles && idx >= 0 && idx < oldCandles.length) ? oldCandles[idx].time : null;
    const mapIdx = oldIdx => {
      const t = oldTimeAt(oldIdx);
      return t == null ? -1 : _timeToNewIndex(newCandles, t);
    };

    // ── 已平仓交易重映射 ──
    let mappedTrades = 0, droppedMarkers = 0;
    trades.forEach(tr => {
      const ne = mapIdx(tr.entryBarIndex);
      const nx = mapIdx(tr.exitBarIndex);
      tr.entryBarIndex = ne;
      tr.exitBarIndex = nx;
      if (ne >= 0 && nx >= 0) mappedTrades++; else droppedMarkers++;
    });

    // ── 持仓子仓位重映射 / 结算 ──
    let keptLots = 0, realizedLots = 0;
    if (position && Array.isArray(position.lots)) {
      const keep = [];
      position.lots.forEach(lot => {
        const ne = mapIdx(lot.entryBarIndex);
        if (ne >= 0) {
          lot.entryBarIndex = ne;
          keep.push(lot);
          keptLots++;
        } else {
          // 无法映射到新周期 → 以切换前价格结算，P&L 落袋，资金进度不丢
          _settleLotToBalance(lot, oldPrice || lot.entryPrice, '周期切换结算');
          realizedLots++;
        }
      });
      position.lots = keep;
      if (position.lots.length === 0) position = null;
      else _recomputeLotTpSl();
    }

    // ── 权益曲线重映射（同一新下标只保留最后一个） ──
    const byIdx = {};
    equityCurve.forEach(p => {
      const t = oldTimeAt(p.index);
      if (t == null) return;
      const ni = _timeToNewIndex(newCandles, t);
      if (ni >= 0 && ni < newVisibleIndex) byIdx[ni] = p.equity;
    });
    equityCurve = Object.keys(byIdx).map(k => ({ index: +k, equity: byIdx[k] }))
      .sort((a, b) => a.index - b.index);

    currentBarIndex = Math.max(0, newVisibleIndex - 1);
    return { keptLots, realizedLots, mappedTrades, droppedMarkers, hasPosition: !!position };
  }

  /** 二分查找：新周期中第一个 time >= oldTime 的下标；超出范围返回 -1 */
  function _timeToNewIndex(newCandles, oldTime) {
    if (!newCandles || !newCandles.length || oldTime == null) return -1;
    let lo = 0, hi = newCandles.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (newCandles[mid].time < oldTime) lo = mid + 1; else hi = mid; }
    return lo >= newCandles.length ? -1 : lo;
  }

  /** 将某子仓位以指定价格结算入 balance，并生成一条平仓记录（用于跨周期无法映射时落袋） */
  function _settleLotToBalance(lot, price, reason) {
    const diff = price - lot.entryPrice;
    const gross = lot.direction === 'long' ? lot.size * diff : lot.size * (-diff);
    const closeValue = lot.size * price;
    const commission = closeValue * cfg.commissionRate;
    const netPnl = gross - commission;
    balance += netPnl;
    if (balance < 0) balance = 0;
    trades.push({
      id: ++tradeIdCounter, lotId: lot.id, direction: lot.direction,
      entryPrice: lot.entryPrice, exitPrice: price,
      size: _r(lot.size, 6), value: _r(closeValue, 4),
      leverage: lot.leverage, layers: _r(lot.layers, 1),
      pnl: _r(netPnl), pnlPct: lot.margin > 0 ? _r(netPnl / lot.margin * 100) : 0,
      commission: _r(commission), margin: _r(lot.margin, 4),
      entryBarIndex: -1, exitBarIndex: -1, barsHeld: 0,
      reason, balance: _r(balance),
    });
  }

  // ══════════════════════════════════════════════════════════
  //  账户快照
  // ══════════════════════════════════════════════════════════

  function getAccountState(currentPrice) {
    const agg = position ? _aggregate() : null;
    const unrealized = position ? _calcUnrealizedPnl(currentPrice) : 0;
    const equity = balance + unrealized;
    const marginUsed = agg ? agg.totalMargin : 0;
    const availableBalance = Math.max(0, balance - marginUsed);
    const marginRatio = agg && agg.totalValue > 0 ? (marginUsed + unrealized) / agg.totalValue : 0;

    return {
      balance: _r(balance),
      equity: _r(equity),
      unrealizedPnl: _r(unrealized),
      unrealizedPnlPct: agg && agg.totalMargin > 0 ? _r(unrealized / agg.totalMargin * 100) : 0,
      marginUsed: _r(marginUsed),
      availableBalance: _r(availableBalance),
      marginRatio: _r(marginRatio * 100),
      liquidationPrice: agg ? _r(agg.liqPrice) : null,
      position: agg ? {
        direction: position.direction,
        entryPrice: _r(agg.avgEntry),
        size: _r(agg.totalSize, 6),
        value: _r(agg.totalValue, 4),
        leverage: _r(agg.leverage, 2),
        layers: _r(agg.totalLayers, 1),
        entryBarIndex: agg.entryBarIndex,
        barsHeld: currentBarIndex - agg.entryBarIndex,
        lotCount: position.lots.length,
        lots: position.lots.map(l => {
          const lu = _lotUnrealized(l, currentPrice);
          return {
            id: l.id, direction: l.direction, layers: _r(l.layers, 1),
            entryPrice: _r(l.entryPrice), size: _r(l.size, 6), value: _r(l.value, 4),
            margin: _r(l.margin, 4), leverage: l.leverage,
            entryBarIndex: l.entryBarIndex, barsHeld: currentBarIndex - l.entryBarIndex,
            tpPrice: l.tpPrice != null ? _r(l.tpPrice) : null,
            slPrice: l.slPrice != null ? _r(l.slPrice) : null,
            liqPrice: _r(l.liqPrice),
            unrealizedPnl: _r(lu),
            unrealizedPnlPct: l.margin > 0 ? _r(lu / l.margin * 100) : 0,
          };
        }),
      } : null,
      currentBarIndex,
    };
  }

  /** 聚合派生量：总数量/保证金/价值/层数、加权均价、有效杠杆、强平价、最早入场下标 */
  function _aggregate() {
    const lots = position.lots;
    let totalSize = 0, totalMargin = 0, totalValue = 0, totalLayers = 0, wsum = 0, minIdx = Infinity;
    lots.forEach(l => {
      totalSize += l.size; totalMargin += l.margin; totalValue += l.value; totalLayers += l.layers;
      wsum += l.size * l.entryPrice;
      if (l.entryBarIndex < minIdx) minIdx = l.entryBarIndex;
    });
    const avgEntry = totalSize > 0 ? wsum / totalSize : 0;
    const leverage = totalMargin > 0 ? totalValue / totalMargin : cfg.leverage;
    const liqPrice = _calcLiqPrice(position.direction, avgEntry, leverage);
    return {
      totalSize, totalMargin, totalValue, totalLayers, avgEntry, leverage, liqPrice,
      entryBarIndex: minIdx === Infinity ? currentBarIndex : minIdx,
    };
  }

  // ══════════════════════════════════════════════════════════
  //  开仓 / 加仓（每次都新增一笔独立子仓位）
  // ══════════════════════════════════════════════════════════

  function openPosition(direction, layers, currentPrice, barIndex) {
    if (currentPrice <= 0) return { ok: false, msg: '价格无效' };
    if (cfg.longOnly && direction === 'short') return { ok: false, msg: '该品种仅支持做多' };
    if (layers < 1 || layers > cfg.maxLayers) return { ok: false, msg: `层数须为 1~${cfg.maxLayers}` };

    // 已有反向持仓 → 先全部平掉（反手），再开新仓
    if (position && position.direction !== direction) {
      const closeResult = _closePosition(currentPrice, barIndex, '反手平仓');
      if (!closeResult.ok) return closeResult;
    }
    return _addLot(direction, layers, currentPrice, barIndex);
  }

  /** 新增一笔子仓位；无持仓时自动建立仓位 */
  function _addLot(direction, layers, currentPrice, barIndex) {
    const agg = position ? _aggregate() : null;
    const currentLayers = agg ? agg.totalLayers : 0;
    if (currentLayers + layers > cfg.maxLayers) {
      return { ok: false, msg: `超过最大层数 ${cfg.maxLayers}（当前 ${_r(currentLayers, 1)} 层）` };
    }

    // 可用余额 = 余额 − 已占用保证金；分仓模式按层数比例分配
    const usedMargin = agg ? agg.totalMargin : 0;
    const availBal = balance - usedMargin;
    const allocRatio = cfg.positionSizing ? (layers / cfg.maxLayers) : 1;
    const allocMargin = availBal * allocRatio;
    if (allocMargin <= 1e-9) return { ok: false, msg: '可用余额不足' };

    const value = allocMargin * cfg.leverage;
    const size = value / currentPrice;
    const commission = value * cfg.commissionRate;

    balance -= commission;
    if (balance < 0) balance = 0;

    const lot = {
      id: ++lotIdCounter, direction, layers,
      entryPrice: currentPrice, size, value, margin: allocMargin,
      leverage: cfg.leverage,
      liqPrice: _calcLiqPrice(direction, currentPrice, cfg.leverage),
      entryBarIndex: barIndex,
      tpPrice: null, slPrice: null,
    };
    _applyLotTpSl(lot);   // 依据当前配置预计算该子仓位的止盈止损价

    if (!position) position = { direction, lots: [lot] };
    else position.lots.push(lot);

    const isFirst = currentLayers === 0;
    return {
      ok: true, lot,
      msg: `${isFirst ? (direction === 'long' ? '做多' : '做空') : '加仓'} ${layers} 层 @ ${_r(currentPrice)}` +
        `（共 ${_r(currentLayers + layers, 1)} 层 / ${position.lots.length} 笔）`,
      commission: _r(commission),
    };
  }

  // ══════════════════════════════════════════════════════════
  //  平仓 / 分批减仓
  // ══════════════════════════════════════════════════════════

  /**
   * @param {Object} [options] 平仓选项
   *   - { lotId }          仅平指定子仓位
   *   - { percent: 1~99 }  按比例部分减仓，剩余继续持有
   *   - 缺省 / percent>=100 全部平仓
   */
  function closePosition(currentPrice, barIndex, reason, options) {
    if (!position) return { ok: false, msg: '无持仓' };
    return _closePosition(currentPrice, barIndex, reason || '手动平仓', options || {});
  }

  function _closePosition(currentPrice, barIndex, reason, options) {
    options = options || {};
    if (!position) return { ok: false, msg: '无持仓' };

    const closed = [];
    if (options.lotId != null) {
      const lot = position.lots.find(l => l.id === options.lotId);
      if (!lot) return { ok: false, msg: '子仓位不存在' };
      closed.push(_closeLot(lot, lot.size, currentPrice, barIndex, reason));
    } else if (options.percent != null && options.percent > 0 && options.percent < 100) {
      const ratio = options.percent / 100;
      position.lots.slice().forEach(lot => {
        const cs = lot.size * ratio;
        if (cs > 1e-12) closed.push(_closeLot(lot, cs, currentPrice, barIndex, `${reason}·减${options.percent}%`));
      });
    } else {
      position.lots.slice().forEach(lot => {
        closed.push(_closeLot(lot, lot.size, currentPrice, barIndex, reason));
      });
    }

    if (position && position.lots.length === 0) position = null;

    let totalPnl = 0, totalCommission = 0, closedMargin = 0;
    closed.forEach(t => { totalPnl += t.pnl; totalCommission += t.commission; closedMargin += (t.margin || 0); });
    _recordEquity(barIndex, currentPrice);

    if (closed.length === 0) return { ok: false, msg: '无可平仓位', totalPnl: 0, trades: [], trade: null };

    const dirText = closed[0].direction === 'long' ? '多' : '空';
    const summaryPct = closedMargin > 0 ? _r(totalPnl / closedMargin * 100) : 0;
    const msg = `${reason} ${dirText}${closed.length > 1 ? ` ${closed.length}笔` : ''} @ ${_r(currentPrice)} ` +
      `盈亏: ${totalPnl >= 0 ? '+' : ''}${_r(totalPnl)} (${summaryPct}%)`;

    return {
      ok: true, msg,
      totalPnl: _r(totalPnl), totalCommission: _r(totalCommission),
      trades: closed,
      trade: { pnl: _r(totalPnl), pnlPct: summaryPct, direction: closed[0].direction },
    };
  }

  /** 平掉某子仓位的一部分（或全部），独立结算其盈亏 */
  function _closeLot(lot, closeSize, currentPrice, barIndex, reason) {
    closeSize = Math.min(closeSize, lot.size);
    const closeRatio = lot.size > 0 ? closeSize / lot.size : 0;
    const closedMargin = lot.margin * closeRatio;
    const closedLayers = lot.layers * closeRatio;

    const diff = currentPrice - lot.entryPrice;
    const gross = lot.direction === 'long' ? closeSize * diff : closeSize * (-diff);
    const closeValue = closeSize * currentPrice;
    const commission = closeValue * cfg.commissionRate;
    const netPnl = gross - commission;

    // 保证金采用“冻结”模型（开仓未从 balance 扣除），平仓只计入净盈亏
    balance += netPnl;
    if (balance < 0) balance = 0;

    // 缩减该子仓位
    lot.size -= closeSize;
    lot.margin -= closedMargin;
    lot.value = lot.size * lot.entryPrice;
    lot.layers -= closedLayers;

    const trade = {
      id: ++tradeIdCounter, lotId: lot.id, direction: lot.direction,
      entryPrice: lot.entryPrice, exitPrice: currentPrice,
      size: _r(closeSize, 6), value: _r(closeValue, 4),
      leverage: lot.leverage, layers: _r(closedLayers, 1),
      pnl: _r(netPnl), pnlPct: closedMargin > 0 ? _r(netPnl / closedMargin * 100) : 0,
      commission: _r(commission), margin: _r(closedMargin, 4),
      entryBarIndex: lot.entryBarIndex, exitBarIndex: barIndex,
      barsHeld: Math.max(0, barIndex - lot.entryBarIndex),
      reason, balance: _r(balance),
    };
    trades.push(trade);

    // 子仓位已平完 → 从持仓移除
    if (lot.size <= 1e-12 && position) {
      position.lots = position.lots.filter(l => l.id !== lot.id);
    }
    return trade;
  }

  // ══════════════════════════════════════════════════════════
  //  逐根K线检查（聚合强平 + 逐仓独立止盈止损）
  // ══════════════════════════════════════════════════════════

  function onBarUpdate(candle, barIndex, indicators) {
    currentBarIndex = barIndex;
    if (!position) { _recordEquity(barIndex, candle.close); return null; }

    const results = [];

    // 1. 聚合强平检查（基于加权均价与总保证金）
    const agg = _aggregate();
    if (position.direction === 'long' && candle.low <= agg.liqPrice) {
      results.push({ type: 'liquidation', ..._closePosition(agg.liqPrice, barIndex, '强制平仓') });
      return results;
    }
    if (position.direction === 'short' && candle.high >= agg.liqPrice) {
      results.push({ type: 'liquidation', ..._closePosition(agg.liqPrice, barIndex, '强制平仓') });
      return results;
    }

    // 2. 逐个子仓位独立检查止盈止损（触发一笔平一笔，互不影响）
    if (cfg.tpSlEnabled) {
      for (const lot of position.lots.slice()) {
        const signal = _checkLotTpSl(lot, candle, indicators);
        if (signal) {
          const t = _closeLot(lot, lot.size, signal.price, barIndex, signal.reason);
          results.push({
            type: 'tp_sl', ok: true, trades: [t], trade: t, totalPnl: t.pnl,
            msg: `子仓#${t.lotId} ${signal.reason} @ ${_r(signal.price)} 盈亏:${t.pnl >= 0 ? '+' : ''}${t.pnl}`,
          });
        }
      }
      if (position && position.lots.length === 0) position = null;
    }

    _recordEquity(barIndex, candle.close);
    return results.length > 0 ? results : null;
  }

  /** 单个子仓位的止盈止损判定（触发价成交，判定与成交自洽） */
  function _checkLotTpSl(lot, candle, indicators) {
    // 百分比模式：用预计算的 tpPrice/slPrice，high/low 触及；同根同时触及时保守先判止损
    if (cfg.tpSlMode === 'percent') {
      if (lot.direction === 'long') {
        if (lot.slPrice != null && candle.low <= lot.slPrice) return { price: lot.slPrice, reason: `止损 ${cfg.slPercent}%` };
        if (lot.tpPrice != null && candle.high >= lot.tpPrice) return { price: lot.tpPrice, reason: `止盈 ${cfg.tpPercent}%` };
      } else {
        if (lot.slPrice != null && candle.high >= lot.slPrice) return { price: lot.slPrice, reason: `止损 ${cfg.slPercent}%` };
        if (lot.tpPrice != null && candle.low <= lot.tpPrice) return { price: lot.tpPrice, reason: `止盈 ${cfg.tpPercent}%` };
      }
      return null;
    }

    // MACD / SAR 模式：可选保护性止损优先，其次信号平仓
    const p = cfg.protectiveSlPercent || 0;
    if (p > 0 && lot.slPrice != null) {
      if (lot.direction === 'long' && candle.low <= lot.slPrice) return { price: lot.slPrice, reason: `保护性止损 ${p}%` };
      if (lot.direction === 'short' && candle.high >= lot.slPrice) return { price: lot.slPrice, reason: `保护性止损 ${p}%` };
    }

    if (cfg.tpSlMode === 'macd' && indicators && indicators.macd) {
      const dif = _getIndicatorValue(indicators.macd.dif, candle.time);
      const dea = _getIndicatorValue(indicators.macd.dea, candle.time);
      const prevDif = _getIndicatorValue(indicators.macd.dif, candle.time, -1);
      const prevDea = _getIndicatorValue(indicators.macd.dea, candle.time, -1);
      if (dif != null && dea != null && prevDif != null && prevDea != null) {
        if (lot.direction === 'long' && prevDif >= prevDea && dif < dea) return { price: candle.close, reason: 'MACD死叉平多' };
        if (lot.direction === 'short' && prevDif <= prevDea && dif > dea) return { price: candle.close, reason: 'MACD金叉平空' };
      }
    } else if (cfg.tpSlMode === 'sar' && indicators && indicators.sar) {
      const sarVal = _getIndicatorValue(indicators.sar, candle.time);
      if (sarVal != null) {
        if (lot.direction === 'long' && candle.close < sarVal) return { price: candle.close, reason: 'SAR反转平多' };
        if (lot.direction === 'short' && candle.close > sarVal) return { price: candle.close, reason: 'SAR反转平空' };
      }
    }
    return null;
  }

  /** 依据当前配置为子仓位预计算止盈/止损价；未启用或对应参数为 0 时置空 */
  function _applyLotTpSl(lot) {
    lot.tpPrice = null; lot.slPrice = null;
    if (!cfg.tpSlEnabled) return;
    const lev = lot.leverage || cfg.leverage;
    // 保证金收益率 → 价格变动幅度需除以杠杆
    if (cfg.tpSlMode === 'percent') {
      const tp = cfg.tpPercent || 0, sl = cfg.slPercent || 0;
      if (lot.direction === 'long') {
        if (tp > 0) lot.tpPrice = lot.entryPrice * (1 + tp / 100 / lev);
        if (sl > 0) lot.slPrice = lot.entryPrice * (1 - sl / 100 / lev);
      } else {
        if (tp > 0) lot.tpPrice = lot.entryPrice * (1 - tp / 100 / lev);
        if (sl > 0) lot.slPrice = lot.entryPrice * (1 + sl / 100 / lev);
      }
    } else {
      const pr = cfg.protectiveSlPercent || 0;
      if (pr > 0) {
        lot.slPrice = lot.direction === 'long'
          ? lot.entryPrice * (1 - pr / 100 / lev)
          : lot.entryPrice * (1 + pr / 100 / lev);
      }
    }
  }

  function _recomputeLotTpSl() {
    if (position) position.lots.forEach(_applyLotTpSl);
  }

  // ══════════════════════════════════════════════════════════
  //  统计
  // ══════════════════════════════════════════════════════════

  function getStatistics() {
    if (trades.length === 0) {
      return {
        totalTrades: 0, winTrades: 0, lossTrades: 0,
        winRate: 0, totalPnl: 0, avgPnl: 0,
        avgWinPnl: 0, avgLossPnl: 0, profitLossRatio: 0,
        maxDrawdown: 0, maxDrawdownPct: 0,
        profitFactor: 0, maxConsecutiveWins: 0, maxConsecutiveLosses: 0,
        avgBarsHeld: 0, sharpe: 0,
        finalBalance: _r(balance), returnPct: 0,
      };
    }

    const wins = trades.filter(t => t.pnl > 0);
    const losses = trades.filter(t => t.pnl <= 0);
    const totalPnl = trades.reduce((s, t) => s + t.pnl, 0);
    const totalWin = wins.reduce((s, t) => s + t.pnl, 0);
    const totalLoss = Math.abs(losses.reduce((s, t) => s + t.pnl, 0));

    // 最大回撤（以每笔平仓后的余额序列计算）
    let maxDD = 0, maxDDPct = 0, peak = cfg.baseAmount;
    const eqArr = [{ eq: cfg.baseAmount }];
    trades.forEach(t => eqArr.push({ eq: t.balance }));
    eqArr.forEach(p => {
      if (p.eq > peak) peak = p.eq;
      const dd = peak - p.eq;
      const ddPct = peak > 0 ? dd / peak * 100 : 0;
      if (dd > maxDD) maxDD = dd;
      if (ddPct > maxDDPct) maxDDPct = ddPct;
    });

    // 连胜连亏
    let maxConsW = 0, maxConsL = 0, consW = 0, consL = 0;
    trades.forEach(t => {
      if (t.pnl > 0) { consW++; consL = 0; maxConsW = Math.max(maxConsW, consW); }
      else { consL++; consW = 0; maxConsL = Math.max(maxConsL, consL); }
    });

    const avgBars = trades.reduce((s, t) => s + t.barsHeld, 0) / trades.length;

    // Sharpe（简化版：以每笔交易收益率为样本）
    const returns = trades.map(t => t.pnlPct);
    const avgRet = returns.reduce((s, v) => s + v, 0) / returns.length;
    const stdRet = Math.sqrt(returns.reduce((s, v) => s + (v - avgRet) ** 2, 0) / returns.length);
    const sharpe = stdRet > 0 ? (avgRet / stdRet) * Math.sqrt(252) : 0;

    return {
      totalTrades: trades.length,
      winTrades: wins.length,
      lossTrades: losses.length,
      winRate: _r(wins.length / trades.length * 100),
      totalPnl: _r(totalPnl),
      avgPnl: _r(totalPnl / trades.length),
      avgWinPnl: _r(wins.length > 0 ? totalWin / wins.length : 0),
      avgLossPnl: _r(losses.length > 0 ? -totalLoss / losses.length : 0),
      profitLossRatio: _r(losses.length > 0 && totalLoss > 0 ? totalWin / totalLoss : (totalWin > 0 ? Infinity : 0)),
      maxDrawdown: _r(maxDD),
      maxDrawdownPct: _r(maxDDPct),
      profitFactor: _r(totalLoss > 0 ? totalWin / totalLoss : (totalWin > 0 ? Infinity : 0)),
      maxConsecutiveWins: maxConsW,
      maxConsecutiveLosses: maxConsL,
      avgBarsHeld: _r(avgBars, 1),
      sharpe: _r(sharpe, 2),
      finalBalance: _r(balance),
      returnPct: _r((balance - cfg.baseAmount) / cfg.baseAmount * 100),
    };
  }

  function getTrades() { return [...trades]; }
  function getEquityCurve() { return [...equityCurve]; }
  function getPosition() { return position ? getAccountState(0).position : null; }

  // ══════════════════════════════════════════════════════════
  //  内部工具
  // ══════════════════════════════════════════════════════════

  function _lotUnrealized(lot, currentPrice) {
    const diff = currentPrice - lot.entryPrice;
    return lot.direction === 'long' ? lot.size * diff : lot.size * (-diff);
  }

  function _calcUnrealizedPnl(currentPrice) {
    if (!position) return 0;
    return position.lots.reduce((s, l) => s + _lotUnrealized(l, currentPrice), 0);
  }

  function _calcLiqPrice(direction, entryPrice, leverage) {
    const mmr = cfg.maintenanceMarginRate;
    if (direction === 'long') {
      return entryPrice * (1 - 1 / leverage + mmr);
    } else {
      return entryPrice * (1 + 1 / leverage - mmr);
    }
  }

  function _recordEquity(barIndex, currentPrice) {
    const unrealized = position ? _calcUnrealizedPnl(currentPrice) : 0;
    const equity = balance + unrealized;
    equityCurve.push({ index: barIndex, equity: _r(equity) });
    if (equity > peakEquity) peakEquity = equity;
  }

  function _getIndicatorValue(arr, time, offset) {
    if (!arr || arr.length === 0) return null;
    offset = offset || 0;
    for (let i = arr.length - 1; i >= 0; i--) {
      if (arr[i].time === time) {
        const idx = i + offset;
        return idx >= 0 && idx < arr.length ? arr[idx].value : null;
      }
    }
    if (offset === 0) return arr[arr.length - 1].value;
    const idx = arr.length - 1 + offset;
    return idx >= 0 && idx < arr.length ? arr[idx].value : null;
  }

  function _r(v, d) {
    d = d != null ? d : 2;
    return Math.round(v * Math.pow(10, d)) / Math.pow(10, d);
  }

  // ── 导出 ──
  return {
    DEFAULT_CFG,
    reset, setConfig, getConfig,
    getBarIndex, setBarIndex, shiftIndices,
    serialize, restore, migrateToPeriod,
    getAccountState, getPosition, openPosition, closePosition,
    onBarUpdate, getStatistics, getTrades, getEquityCurve,
  };
})();
