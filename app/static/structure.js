(() => {
  const get = id => document.getElementById(id);
  const format = value => value == null ? 'Unavailable' : typeof value === 'number'
    ? value.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : String(value);
  function paint(id, metrics) {
    const container = get(id);
    container.replaceChildren();
    const rows = [
      ['Spot', metrics.spot], ['Future', metrics.future], ['Contract', metrics.future_symbol],
      ['Futures basis', metrics.futures_basis], ['Futures VWAP', metrics.futures_vwap],
      ['Price vs futures VWAP', metrics.price_vs_vwap], ['VWAP source', metrics.vwap_method],
      ['Spot day open', metrics.day_open], ['Spot day high', metrics.day_high], ['Spot day low', metrics.day_low],
      ['Previous session', metrics.previous_session_date], ['Previous day high', metrics.previous_day_high],
      ['Previous day low', metrics.previous_day_low], ['Previous day close', metrics.previous_day_close],
      ['Opening range high', metrics.opening_range_high], ['Opening range low', metrics.opening_range_low],
      ['Spot vs opening range', metrics.opening_range_state],
      ...['5m', '15m', '30m'].flatMap(interval => [
        [interval + ' spot EMA9', metrics[interval]?.ema9], [interval + ' spot EMA20', metrics[interval]?.ema20]])
    ];
    const status = document.createElement('p');
    status.textContent = metrics.stale ? 'STALE / MARKET CLOSED' : 'LIVE ●';
    status.className = metrics.stale ? 'stale' : 'live';
    container.append(status);
    const list = document.createElement('dl');
    for (const [label, value] of rows) {
      const term = document.createElement('dt'), detail = document.createElement('dd');
      term.textContent = label; detail.textContent = format(value);
      list.append(term, detail);
    }
    container.append(list);
  }
  let busy = false;
  async function refreshStructure() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/market/structure', {signal: AbortSignal.timeout(12000)});
      if (!response.ok) throw new Error();
      const data = await response.json();
      paint('nifty-structure', data.nifty);
      paint('banknifty-structure', data.banknifty);
      get('structure-status').textContent = 'History: ' + data.recovery_status.replaceAll('_', ' ') +
        (data.dropped_tick_events ? ' · Tick gaps detected; affected candles require history recovery.' : '');
    } catch {
      get('structure-status').textContent = 'Market structure unavailable. Retrying shortly.';
      get('nifty-structure').replaceChildren(); get('banknifty-structure').replaceChildren();
    } finally { busy = false; }
  }
  refreshStructure();
  setInterval(refreshStructure, 5000);
})();
