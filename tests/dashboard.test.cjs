// Run with node --test tests/dashboard.test.cjs (no npm dependencies).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

async function render(connection, live, rest) {
  const elements = {};
  const calls = [];
  const context = vm.createContext({
    document: {getElementById(id) { return elements[id] ||= {textContent: '', addEventListener() {}}; }},
    AbortSignal, Date, setInterval() {},
    fetch: async url => {
      calls.push(url);
      const data = url === '/api/connection' ? connection : url === '/api/market/live' ? live : rest;
      if (data instanceof Error) throw data;
      return {ok: true, json: async () => data};
    }
  });
  vm.runInContext(fs.readFileSync('app/static/dashboard.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return {elements, calls};
}
const connection = {configured: true, connection_status: 'session_available'};
const time = '2026-09-24T10:00:00Z';
const instruments = ['NIFTY 50', 'NIFTY BANK', 'INDIA VIX'].map(symbol => ({symbol, last_price: 100, last_tick_received_at: time, stale: false}));

test('live ticks use live endpoint without REST calls', async () => {
  const {elements, calls} = await render(connection, {status: 'connected', websocket_status: 'connected', stale: false, instruments, last_tick_received_at: time});
  assert.equal(elements.freshness.textContent, 'LIVE ●');
  assert.equal(elements.nifty.textContent, '100.00');
  assert.equal(elements.status.textContent, 'CONNECTED');
  assert.equal(calls.length, 2);
});
test('stale ticks remain distinct from WebSocket connectivity', async () => {
  const {elements} = await render(connection, {status: 'stale', websocket_status: 'connected', stale: true, instruments: instruments.map(i => ({...i, stale: true}))});
  assert.equal(elements.socket.textContent, 'CONNECTED');
  assert.equal(elements.feed.textContent, 'STALE');
  assert.equal(elements['nifty-source'].textContent, 'STALE / MARKET CLOSED');
});
test('REST fallback does not fabricate last tick', async () => {
  const {elements, calls} = await render(connection, {status: 'connecting', websocket_status: 'connecting', stale: true, instruments: []}, {instruments: [{name: 'NIFTY 50', value: 110}], last_updated: time});
  assert.equal(elements.nifty.textContent, '110.00');
  assert.equal(elements['nifty-tick'].textContent, '—');
  assert.match(elements['nifty-source'].textContent, /REST fallback/);
  assert.equal(calls.at(-1), '/api/market/snapshot');
});
test('authentication required clears prices', async () => {
  const {elements, calls} = await render(connection, {status: 'authentication_required', websocket_status: 'authentication_required', stale: true, instruments});
  assert.equal(elements.status.textContent, 'DISCONNECTED');
  assert.equal(elements.nifty.textContent, '—');
  assert.equal(calls.length, 2);
});
test('HTTP failure clears live indicators', async () => {
  const {elements} = await render(new Error('Unavailable'));
  assert.equal(elements.nifty.textContent, '—');
  assert.equal(elements.feed.textContent, 'UNAVAILABLE');
  assert.equal(elements.freshness.textContent, 'STALE / MARKET CLOSED');
});
