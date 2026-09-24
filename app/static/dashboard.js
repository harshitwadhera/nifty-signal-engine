const el = id => document.getElementById(id);
let busy = false;
async function refresh() {
  if (busy) return;
  busy = true;
  el('refresh').disabled = true;
  try {
    const connectionResponse = await fetch('/api/connection');
    if (!connectionResponse.ok) throw new Error('Connection check unavailable.');
    const connection = await connectionResponse.json();
    if (!connection.configured || connection.connection_status === 'disconnected') {
      el('status').textContent = 'Disconnected';
      throw new Error(connection.configured ? 'Connect to Zerodha to load quotes.' : 'Add your Kite credentials to .env and restart the application.');
    }
    const response = await fetch('/api/market/snapshot');
    const data = await response.json();
    if (!response.ok) {
      el('status').textContent = response.status === 401 ? 'Disconnected' : 'Market data unavailable';
      throw new Error(data.error?.message || 'Unable to load market data.');
    }
    el('status').textContent = 'Connected';
    const ids = {'NSE:NIFTY 50': 'nifty', 'NSE:NIFTY BANK': 'bank', 'NSE:INDIA VIX': 'vix'};
    for (const item of data.instruments) {
      el(ids[item.symbol]).textContent = item.value === null ? 'Unavailable' : item.value.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }
    el('updated').textContent = new Date(data.last_updated).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST';
    el('message').textContent = data.partial ? 'Some index quotes are unavailable.' : '';
  } catch (error) {
    for (const id of ['nifty', 'bank', 'vix', 'updated']) el(id).textContent = '—';
    if (el('status').textContent === 'Connected' || el('status').textContent.startsWith('Checking')) el('status').textContent = 'Connection unavailable';
    el('message').textContent = error.message;
  } finally { busy = false; el('refresh').disabled = false; }
}
el('refresh').addEventListener('click', refresh);
refresh();
setInterval(refresh, 15000);
