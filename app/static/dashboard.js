const el = id => document.getElementById(id);
const ids = {'NIFTY 50': 'nifty', 'NIFTY BANK': 'bank', 'INDIA VIX': 'vix'};
const stamp = value => value ? new Date(value).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST' : '—';
const price = value => value == null ? 'Unavailable' : value.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
let busy = false, fallback = null, lastFallbackAttempt = 0;
async function get(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(12000)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || 'Market data unavailable.');
  return data;
}
function clearValues() {
  for (const id of Object.values(ids)) {
    el(id).textContent = '—'; el(id + '-tick').textContent = '—';
    el(id + '-source').textContent = 'Waiting for data';
  }
  el('updated').textContent = '—';
}
async function refresh() {
  if (busy) return;
  busy = true;
  el('refresh').disabled = true;
  try {
    const connection = await get('/api/connection');
    const live = await get('/api/market/live');
    const authenticated = connection.configured && connection.connection_status !== 'disconnected' && live.websocket_status !== 'authentication_required';
    el('status').textContent = authenticated ? 'CONNECTED' : 'DISCONNECTED';
    el('feed').textContent = live.status.replaceAll('_', ' ').toUpperCase();
    el('socket').textContent = live.websocket_status.replaceAll('_', ' ').toUpperCase();
    el('freshness').textContent = live.stale ? 'STALE / MARKET CLOSED' : 'LIVE ●';
    el('freshness').className = live.stale ? 'stale' : 'live';
    if (!authenticated) {
      fallback = null; clearValues();
      el('message').textContent = connection.configured ? 'Connect to Zerodha to start the market feed.' : 'Add Kite credentials to .env and restart.';
      return;
    }
    let fallbackError = '';
    const needsFallback = Object.keys(ids).some(symbol => !live.instruments.some(i => i.symbol === symbol && i.last_price != null));
    if (needsFallback && Date.now() - lastFallbackAttempt >= 15000) {
      lastFallbackAttempt = Date.now();
      try { fallback = await get('/api/market/snapshot'); }
      catch (error) { fallback = null; fallbackError = error.message; }
    }
    clearValues();
    for (const [symbol, id] of Object.entries(ids)) {
      const item = live.instruments.find(i => i.symbol === symbol);
      if (item?.last_price != null) {
        el(id).textContent = price(item.last_price);
        el(id + '-tick').textContent = stamp(item.last_tick_received_at);
        el(id + '-source').textContent = item.stale ? 'STALE / MARKET CLOSED' : 'LIVE ●';
      } else {
        const rest = fallback?.instruments.find(i => i.name === symbol);
        if (rest) {
          el(id).textContent = price(rest.value);
          el(id + '-source').textContent = 'REST fallback · retrieved ' + stamp(fallback.last_updated);
        }
      }
    }
    el('updated').textContent = stamp(live.last_tick_received_at);
    el('message').textContent = fallbackError || (live.stale ? 'No fresh ticks. The market may be closed; connectivity is shown separately.' : '');
  } catch (error) {
    clearValues(); fallback = null;
    el('status').textContent = 'UNKNOWN'; el('feed').textContent = 'UNAVAILABLE';
    el('socket').textContent = 'UNKNOWN'; el('freshness').textContent = 'STALE / MARKET CLOSED';
    el('freshness').className = 'stale';
    el('message').textContent = error.message;
  } finally { busy = false; el('refresh').disabled = false; }
}
el('refresh').addEventListener('click', refresh);
refresh();
setInterval(refresh, 2000);
