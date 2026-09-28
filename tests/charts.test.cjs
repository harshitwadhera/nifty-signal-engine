const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {node} = require('./helpers/dom.cjs');
const flush = () => new Promise(resolve => setImmediate(resolve));
const pending = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function payload(symbol = 'NIFTY', interval = '5m', count = 3) {
  const duration = Number.parseInt(interval) * 60000, end = Date.parse('2026-09-28T10:00:00+05:30');
  return {symbol: symbol === 'NIFTY' ? 'NIFTY 50' : 'NIFTY BANK', interval,
    candles: Array.from({length: count}, (_, i) => ({
      start_time: new Date(end - (count - 1 - i) * duration).toISOString(),
      end_time: new Date(end - (count - 2 - i) * duration).toISOString(),
      open: 100 + i, high: 110 + i, low: 90 + i, close: 105 + i,
      completed: i !== count - 1, partial: i === 0, source: i === 0 ? 'historical' : 'live'
    }))};
}
const response = data => ({ok: true, json: async () => data});
async function page(now = '2026-09-28T10:01:00+05:30', request) {
  const elements = {}, timers = [], calls = [], observers = [], resizes = [], state = {now, request};
  const get = id => elements[id] ||= node();
  get('status').textContent = 'CONNECTED'; get('freshness').className = 'live';
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [state.now])); }
    static now() { return new Date(state.now).getTime(); }
  }
  const context = vm.createContext({window: {}, Date: Clock, AbortSignal, AbortController,
    document: {getElementById: get, createElementNS: (_, tag) => node(tag)},
    ResizeObserver: class { constructor(fn) { resizes.push(fn); } observe() {} },
    MutationObserver: class { constructor(fn) { observers.push(fn); } observe() {} },
    setInterval: (fn, ms) => timers.push({fn, ms}),
    fetch: async (url, options) => {
      calls.push({url, signal: options.signal});
      const parsed = new URL(url, 'http://test');
      return state.request ? state.request(url, options) : response(payload(parsed.pathname.split('/').at(-1), parsed.searchParams.get('interval')));
    }
  });
  for (const file of ['market_hours.js', 'charts.js']) vm.runInContext(fs.readFileSync('app/static/' + file, 'utf8'), context);
  await flush();
  return {get, calls, timers, state, observers, resizes,
    root: (id = 'nifty') => get(id + '-chart-frame').children[0],
    bars: (id = 'nifty') => get(id + '-chart-frame').children[0].children[1].children,
    status: (id = 'nifty') => get(id + '-chart-status').textContent,
    poll: async () => { timers[0].fn(); await flush(); },
    switch: async (interval, id = 'nifty') => {
      const select = get(id + '-chart-interval'); select.value = interval;
      select.listeners.change({target: select}); await flush();
    }};
}

test('both charts load once after hours, use one 5-second timer, and resume only in the IST window', async () => {
  const h = await page('2026-09-28T08:59:59+05:30');
  assert.deepEqual(h.calls.map(c => c.url), ['/api/candles/NIFTY?interval=5m&limit=100', '/api/candles/BANKNIFTY?interval=5m&limit=100']);
  assert.deepEqual(h.timers.map(t => t.ms), [5000]);
  for (const [now, count] of [['2026-09-28T08:59:59+05:30', 0], ['2026-09-28T09:00:00+05:30', 2],
    ['2026-09-28T15:39:59+05:30', 2], ['2026-09-28T15:40:00+05:30', 0],
    ['2026-10-03T10:00:00+05:30', 0], ['2026-10-04T10:00:00+05:30', 0]]) {
    h.calls.length = 0; h.state.now = now; await h.poll(); assert.equal(h.calls.length, count, now);
    if (count) assert.deepEqual(h.calls.map(c => c.url), [
      '/api/candles/NIFTY?interval=5m&limit=3', '/api/candles/BANKNIFTY?interval=5m&limit=3'
    ]);
  }
});


test('automatic refresh merges only recent candles and keeps the chart bounded', async () => {
  const initial = payload('NIFTY', '5m', 100);
  const bank = payload('BANKNIFTY', '5m', 100);
  const h = await page('2026-09-28T10:01:00+05:30', async url => {
    const parsed = new URL(url, 'http://test');
    const isBank = parsed.pathname.includes('BANKNIFTY');
    const limit = Number(parsed.searchParams.get('limit'));
    const source = isBank ? bank : initial;
    return response({...source, candles: source.candles.slice(-limit)});
  });
  assert.equal(h.bars().length, 100);
  h.calls.length = 0;
  const formingKey = h.bars().at(-1).getAttribute('data-start');
  initial.candles.at(-1).close = 999;
  await h.poll();
  assert.deepEqual(h.calls.map(c => c.url), [
    '/api/candles/NIFTY?interval=5m&limit=3', '/api/candles/BANKNIFTY?interval=5m&limit=3'
  ]);
  assert.equal(h.bars().length, 100);
  assert.equal(h.bars().at(-1).getAttribute('data-start'), formingKey);
  assert.match(h.bars().at(-1).children[0].textContent, /C 999.00/);

  initial.candles.at(-1).completed = true;
  const last = initial.candles.at(-1);
  initial.candles.push({...last, start_time: last.end_time,
    end_time: new Date(Date.parse(last.end_time) + 300000).toISOString(),
    open: 999, high: 1002, low: 998, close: 1001, completed: false, partial: false, source: 'live'});
  await h.poll();
  assert.equal(h.bars().length, 100);
  assert.match(h.bars().at(-1).children[0].textContent, /C 1,001.00/);
  assert.match(h.bars().at(-1).className, /chart-forming/);
});

test('incremental correction updates authoritative candle values without replacing its DOM node', async () => {
  const initial = payload('NIFTY', '5m', 5);
  const h = await page('2026-09-28T10:01:00+05:30', async url => {
    const parsed = new URL(url, 'http://test');
    const source = parsed.pathname.includes('BANKNIFTY') ? payload('BANKNIFTY', '5m', 5) : initial;
    return response({...source, candles: source.candles.slice(-Number(parsed.searchParams.get('limit')))});
  });
  const target = h.bars().at(-2), key = target.getAttribute('data-start');
  initial.candles.at(-2).close = 108.5;
  initial.candles.at(-2).partial = false;
  initial.candles.at(-2).source = 'historical';
  await h.poll();
  const corrected = h.bars().find(bar => bar.getAttribute('data-start') === key);
  assert.equal(corrected, target);
  assert.match(corrected.children[0].textContent, /C 108.50/);
  assert.doesNotMatch(corrected.className, /chart-partial/);
});

test('incremental gaps or malformed recent data fall back to a full 100-candle refresh', async () => {
  const h = await page();
  h.calls.length = 0;
  const full = payload('NIFTY', '5m', 4);
  const gap = payload('NIFTY', '5m', 3);
  for (const candle of gap.candles) {
    candle.start_time = new Date(Date.parse(candle.start_time) + 3600000).toISOString();
    candle.end_time = new Date(Date.parse(candle.end_time) + 3600000).toISOString();
  }
  h.state.request = async url => {
    const parsed = new URL(url, 'http://test');
    if (parsed.pathname.includes('BANKNIFTY')) return response(payload('BANKNIFTY'));
    return response(Number(parsed.searchParams.get('limit')) === 3 ? gap : full);
  };
  await h.poll();
  assert.deepEqual(h.calls.filter(c => c.url.includes('/NIFTY?')).map(c => c.url), [
    '/api/candles/NIFTY?interval=5m&limit=3',
    '/api/candles/NIFTY?interval=5m&limit=100'
  ]);
  assert.equal(h.bars().length, 4);

  h.calls.length = 0;
  const malformed = {...payload(), candles: [{...payload().candles[0], start_time: '2026-09-28T09:15:00'}]};
  h.state.request = async url => {
    const parsed = new URL(url, 'http://test');
    if (parsed.pathname.includes('BANKNIFTY')) return response(payload('BANKNIFTY'));
    return response(Number(parsed.searchParams.get('limit')) === 3 ? malformed : full);
  };
  await h.poll();
  assert.deepEqual(h.calls.filter(c => c.url.includes('/NIFTY?')).map(c => c.url), [
    '/api/candles/NIFTY?interval=5m&limit=3',
    '/api/candles/NIFTY?interval=5m&limit=100'
  ]);
});


test('zoom, wheel, horizontal pan and reset stay client-side and preserve a panned history view during live updates', async () => {
  const nifty = payload('NIFTY', '5m', 100);
  const bank = payload('BANKNIFTY', '5m', 100);
  const h = await page('2026-09-28T10:01:00+05:30', async url => {
    const parsed = new URL(url, 'http://test');
    const source = parsed.pathname.includes('BANKNIFTY') ? bank : nifty;
    return response({...source, candles: source.candles.slice(-Number(parsed.searchParams.get('limit')))});
  });
  h.calls.length = 0;
  assert.equal(h.bars().length, 100);

  await h.get('nifty-chart-zoom-in').listeners.click();
  assert.equal(h.bars().length, 60);
  await h.get('nifty-chart-zoom-in').listeners.click();
  assert.equal(h.bars().length, 30);

  let wheelPrevented = false;
  h.root().listeners.wheel({deltaY: -1, preventDefault() { wheelPrevented = true; }});
  assert.equal(wheelPrevented, true);
  assert.equal(h.bars().length, 15);
  await h.get('nifty-chart-zoom-out').listeners.click();
  assert.equal(h.bars().length, 30);
  assert.deepEqual(h.calls, []);

  const latestBeforePan = h.bars().at(-1).getAttribute('data-start');
  h.root().listeners.pointerdown({button: 0, clientX: 100});
  h.root().listeners.pointermove({clientX: 212, preventDefault() {}});
  h.root().listeners.pointerup({});
  const pannedRightEdge = h.bars().at(-1).getAttribute('data-start');
  assert.ok(Number(pannedRightEdge) < Number(latestBeforePan));
  assert.match(h.get('nifty-chart-quality').textContent, /View: 30\/100 history/);

  nifty.candles.at(-1).completed = true;
  const last = nifty.candles.at(-1);
  nifty.candles.push({...last, start_time: last.end_time,
    end_time: new Date(Date.parse(last.end_time) + 300000).toISOString(),
    open: 205, high: 210, low: 200, close: 208, completed: false, partial: false, source: 'live'});
  await h.poll();
  assert.equal(h.bars().at(-1).getAttribute('data-start'), pannedRightEdge);
  assert.match(h.get('nifty-chart-quality').textContent, /history/);

  await h.get('nifty-chart-reset').listeners.click();
  assert.equal(h.bars().length, 100);
  assert.equal(h.bars().at(-1).getAttribute('data-start'), String(Date.parse(nifty.candles.at(-1).start_time)));
  assert.match(h.get('nifty-chart-quality').textContent, /latest/);
  assert.equal(h.get('nifty-chart-reset').disabled, true);
});

test('timeframe controls and manual refresh fetch only their chart immediately, even on weekends', async () => {
  const h = await page('2026-10-03T10:00:00+05:30'); h.calls.length = 0;
  await h.switch('15m'); await h.switch('30m', 'banknifty');
  await h.get('nifty-chart-refresh').listeners.click();
  assert.deepEqual(h.calls.map(c => c.url), ['/api/candles/NIFTY?interval=15m&limit=100',
    '/api/candles/BANKNIFTY?interval=30m&limit=100', '/api/candles/NIFTY?interval=15m&limit=100']);
  await h.switch('15m'); await h.switch('10m'); await h.poll();
  assert.equal(h.calls.length, 3); assert.equal(h.timers.length, 1);
  assert.match(h.status(), /Showing 15m/); assert.match(h.status('banknifty'), /Showing 30m/);
});

test('forming OHLC updates in place, then closes and a new candle appears without rewriting old OHLC', async () => {
  const h = await page(), root = h.root(), old = h.bars()[0], forming = h.bars().at(-1);
  assert.match(forming.className, /chart-forming/); assert.match(old.className, /chart-partial/);
  assert.match(old.children[0].textContent, /historical.*PARTIAL/s);
  assert.equal(old.children[4].getAttribute('visibility'), 'visible');
  const oldTitle = old.children[0].textContent, oldBody = {...old.children[3].attributes};
  const data = payload(); data.candles.at(-1).close = 109;
  h.state.request = async () => response(data); await h.get('nifty-chart-refresh').listeners.click();
  assert.equal(h.root(), root); assert.equal(h.bars().at(-1), forming);
  assert.match(forming.children[0].textContent, /C 109.00/);
  assert.equal(old.children[0].textContent, oldTitle);
  assert.deepEqual(old.children[3].attributes, oldBody);
  data.candles.at(-1).completed = true;
  data.candles.push({...data.candles.at(-1), start_time: data.candles.at(-1).end_time,
    end_time: '2026-09-28T10:10:00+05:30', completed: false});
  await h.get('nifty-chart-refresh').listeners.click();
  assert.doesNotMatch(forming.className, /chart-forming/); assert.equal(h.bars().length, 4);
  assert.match(h.bars().at(-1).className, /chart-forming/);
  assert.equal(old.children[0].textContent, oldTitle);
});

test('in-flight refreshes never overlap, stale timeframe responses are ignored, latest selection wins', async () => {
  const first = pending(), second = pending();
  const h = await page('2026-10-03T10:00:00+05:30', url => url.includes('NIFTY?') && !url.includes('BANK') ? first.promise : response(payload('BANKNIFTY')));
  assert.equal(h.calls.length, 2);
  await h.get('nifty-chart-refresh').listeners.click();
  await h.switch('15m'); await h.switch('30m');
  assert.equal(h.calls.length, 2); assert.equal(h.calls[0].signal.aborted, true);
  h.state.request = () => second.promise;
  first.resolve(response(payload())); await flush();
  assert.equal(h.calls.length, 3); assert.match(h.calls.at(-1).url, /interval=30m/);
  assert.equal(h.bars().length, 0);
  second.resolve(response(payload('NIFTY', '30m'))); await flush();
  assert.match(h.status(), /Showing 30m/); assert.equal(h.timers.length, 1);
  h.state.now = '2026-09-28T10:00:00+05:30';
  const stalled = pending(); h.state.request = () => stalled.promise;
  await h.poll(); const count = h.calls.length; await h.poll();
  await h.get('nifty-chart-refresh').listeners.click(); assert.equal(h.calls.length, count);
  stalled.resolve(response(payload())); await flush();
});

test('failed, empty and malformed refreshes retain the last successful candles and displayed interval', async () => {
  const h = await page(), root = h.root(), bars = [...h.bars()];
  for (const value of [{ok: false, status: 503}, response({...payload(), candles: []}),
    response({...payload(), interval: '15m'}), response({...payload(), candles: [{...payload().candles[0], start_time: '2026-09-28T09:15:00'}]})]) {
    h.state.request = async () => value; await h.get('nifty-chart-refresh').listeners.click();
    assert.equal(h.root(), root); assert.deepEqual(h.bars(), bars); assert.match(h.status(), /Showing saved chart; it may be stale/);
  }
  h.state.request = async () => ({ok: false, status: 503}); await h.switch('15m');
  assert.match(h.get('nifty-chart-price').textContent, /5m/); assert.match(h.status(), /Showing 5m/);
  h.state.request = async () => response(payload('NIFTY', '15m')); await h.get('nifty-chart-refresh').listeners.click();
  assert.match(h.status(), /Showing 15m/); assert.doesNotMatch(h.status(), /may be stale/);
});

test('initial empty, authentication failure and network failure have clear retry states', async () => {
  const h = await page(undefined, async () => response({...payload(), candles: []}));
  assert.match(h.status(), /No candle history yet/);
  assert.equal(h.root().children.at(-1).textContent, 'No candle history yet');
  h.get('status').textContent = 'DISCONNECTED'; h.observers[0](); assert.match(h.status(), /Zerodha disconnected/);
  h.state.request = async () => ({ok: false, status: 401}); await h.get('nifty-chart-refresh').listeners.click();
  assert.match(h.status(), /Connect to Zerodha to load candles/); assert.equal(h.bars().length, 0);
  h.resizes[0](); assert.equal(h.root().children.at(-1).textContent, 'Candles unavailable');
  h.state.request = async () => { throw new Error('Network unavailable'); }; await h.get('nifty-chart-refresh').listeners.click();
  assert.match(h.status(), /Use Refresh chart to retry/);
  h.state.request = async () => response(payload()); await h.get('nifty-chart-refresh').listeners.click();
  assert.equal(h.bars().length, 3);
});

test('bounded chart nodes, resize, keyboard inspection, and recovery corrections trust server OHLC', async () => {
  const h = await page(); const root = h.root();
  root.listeners.keydown({key: 'Home', preventDefault() {}});
  assert.match(h.get('nifty-chart-readout').textContent, /PARTIAL/);
  root.listeners.keydown({key: 'End', preventDefault() {}});
  assert.match(h.get('nifty-chart-readout').textContent, /Forming/);
  const data = payload('NIFTY', '5m', 100); data.candles[0].close = 104;
  h.state.request = async () => response(data); await h.get('nifty-chart-refresh').listeners.click();
  for (let i = 0; i < 3; i++) {
    data.candles.shift(); const last = data.candles.at(-1);
    data.candles.push({...last, start_time: last.end_time, end_time: new Date(Date.parse(last.end_time) + 300000).toISOString()});
    await h.get('nifty-chart-refresh').listeners.click(); assert.equal(h.bars().length, 100);
  }
  const key = String(Date.parse(data.candles[0].start_time));
  const first = h.bars().find(bar => bar.getAttribute('data-start') === key);
  data.candles[0].partial = false; data.candles[0].source = 'historical'; data.candles[0].close = 104;
  await h.get('nifty-chart-refresh').listeners.click(); assert.equal(h.bars().find(bar => bar.getAttribute('data-start') === key), first);
  assert.match(first.children[0].textContent, /C 104.00/); assert.doesNotMatch(first.className, /chart-partial/);
  h.get('nifty-chart-frame').clientWidth = 280; h.resizes[0]();
  assert.equal(h.root(), root); assert.equal(root.getAttribute('viewBox'), '0 0 280 282'); assert.equal(h.timers.length, 1);
});

test('IST axis and aware timestamp parsing are independent of system timezone', async () => {
  const oldTZ = process.env.TZ;
  try {
    for (const tz of ['UTC', 'America/Los_Angeles', 'Asia/Kolkata']) {
      process.env.TZ = tz;
      const h = await page();
      assert.match(h.get('nifty-chart-readout').textContent, /28 Sept? 10:00 IST/);
      assert.equal(h.root().children[0].children[14].textContent, '10:00');
      const data = payload(); data.candles.at(-1).start_time = '2026-09-28T10:00:00+05:30';
      h.state.request = async () => response(data); await h.get('nifty-chart-refresh').listeners.click();
      assert.match(h.get('nifty-chart-readout').textContent, /10:00 IST/);
    }
  } finally { if (oldTZ === undefined) delete process.env.TZ; else process.env.TZ = oldTZ; }
});

test('one/two candles and flat prices render finite geometry without duplicate time labels', async () => {
  for (const count of [1, 2]) {
    const data = payload('NIFTY', '5m', count);
    for (const c of data.candles) c.open = c.high = c.low = c.close = 100;
    const h = await page(undefined, async url => response(url.includes('BANK') ? payload('BANKNIFTY') : data));
    assert.equal(h.bars().length, count);
    for (const bar of h.bars()) {
      const body = bar.children[3];
      for (const key of ['x', 'y', 'width', 'height']) assert.ok(Number.isFinite(Number(body.getAttribute(key))));
      assert.ok(Number(body.getAttribute('height')) >= 1);
    }
    const times = h.root().children[0].children.slice(10).filter((_, i) => i % 2 === 0);
    assert.equal(times.filter(n => n.getAttribute('visibility') === 'visible').length, count);
  }
});

test('an aborted switch and a timed-out request both settle and allow a retry', async () => {
  let started;
  const h = await page('2026-10-03T10:00:00+05:30', (url, {signal}) => {
    if (url.includes('BANK')) return response(payload('BANKNIFTY'));
    started = true;
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), {once: true}));
  });
  assert.equal(started, true);
  assert.equal(h.root().children.at(-1).textContent, 'Loading candles…');
  h.state.request = async () => response(payload('NIFTY', '15m')); await h.switch('15m');
  assert.match(h.status(), /Showing 15m/); assert.equal(h.calls.length, 3);
  h.state.request = async () => { throw new DOMException('Timed out', 'TimeoutError'); };
  await h.get('nifty-chart-refresh').listeners.click();
  assert.match(h.status(), /timed out.*saved chart/); assert.equal(h.get('nifty-chart-refresh').disabled, false);
  h.state.request = async () => response(payload('NIFTY', '15m')); await h.get('nifty-chart-refresh').listeners.click();
  assert.doesNotMatch(h.status(), /timed out/);
});
