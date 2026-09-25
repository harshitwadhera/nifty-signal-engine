const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function node() {
  return {textContent: '', children: [], append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children = items;}};
}
async function render(data, ok = true) {
  const elements = {};
  const context = vm.createContext({
    document: {getElementById: id => elements[id] ||= node(), createElement: node},
    AbortSignal, setInterval() {},
    fetch: async url => {assert.equal(url, '/api/market/structure'); return {ok, json: async () => data};}
  });
  vm.runInContext(fs.readFileSync('app/static/structure.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return elements;
}
test('structure panel distinguishes spot, futures, VWAP and missing metrics', async () => {
  const metrics = {spot: 23000, future: 23020, futures_vwap: 23010,
    future_symbol: '<script>never executable</script>', stale: false,
    vwap_method: 'exchange_session_average', '5m': {ema9: 22990, ema20: 22980}};
  const elements = await render({nifty: metrics, banknifty: {}, recovery_status: 'ready', dropped_tick_events: 0});
  const fields = elements['nifty-structure'].children[1].children.map(n => n.textContent);
  assert.ok(fields.includes('Futures VWAP'));
  assert.ok(fields.includes('Spot day high'));
  assert.ok(fields.includes('Unavailable'));
  assert.ok(fields.includes('<script>never executable</script>'));
  assert.equal(elements['structure-status'].textContent, 'History: ready');
});
test('structure failure clears displayed panels', async () => {
  const elements = await render({}, false);
  assert.equal(elements['nifty-structure'].children.length, 0);
  assert.match(elements['structure-status'].textContent, /unavailable/);
});
test('data gaps and staleness are visible', async () => {
  const elements = await render({nifty: {stale: true}, banknifty: {stale: true}, recovery_status: 'history_unavailable', dropped_tick_events: 2});
  assert.equal(elements['nifty-structure'].children[0].textContent, 'STALE / MARKET CLOSED');
  assert.match(elements['structure-status'].textContent, /Tick gaps detected/);
});
