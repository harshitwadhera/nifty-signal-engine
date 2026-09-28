const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {node} = require('./helpers/dom.cjs');

const flush = () => new Promise(resolve => setImmediate(resolve));
const scripts = [...fs.readFileSync('app/static/index.html', 'utf8')
  .matchAll(/<script\b([^>]*?)src="\/static\/([^"]+)"([^>]*)><\/script>/g)];
async function page(now, missingTicks=false) {
  const elements={}, calls=[], timers=[];
  const state={now};
  class Clock extends Date {
    constructor(...args){super(...(args.length ? args : [state.now]));}
    static now(){return new Date(state.now).getTime();}
  }
  const data={
    '/api/connection':{configured:true, connection_status:'session_available'},
    '/api/market/live':{status:'connected', websocket_status:'connected', stale:false,
      instruments:missingTicks ? [] : ['NIFTY 50','NIFTY BANK','INDIA VIX'].map(symbol=>({symbol,last_price:100,stale:false}))},
    '/api/market/snapshot':{instruments:[]},
    '/api/market/structure':{nifty:{},banknifty:{},recovery_status:'ready'},
    '/api/signals/current':{signals:['NIFTY','BANKNIFTY'].map(index=>({index,decision:'NO_TRADE'}))},
    '/api/options/nifty':{expiries:['2026-10-06']},
    '/api/options/banknifty':{expiries:['2026-10-27']},
    '/api/candles/NIFTY':{symbol:'NIFTY 50',interval:'5m',candles:[{
      start_time:'2026-09-28T09:55:00+05:30',end_time:'2026-09-28T10:00:00+05:30',
      open:100,high:101,low:99,close:100.5,completed:false,partial:false,source:'live'}]},
    '/api/candles/BANKNIFTY':{symbol:'NIFTY BANK',interval:'5m',candles:[{
      start_time:'2026-09-28T09:55:00+05:30',end_time:'2026-09-28T10:00:00+05:30',
      open:100,high:101,low:99,close:100.5,completed:false,partial:false,source:'live'}]},
    '/api/trades/active':{items:[]},
    '/api/trades/setup/nifty':{setup_state:'NO_TRADE'},
    '/api/trades/setup/banknifty':{setup_state:'NO_TRADE'}
  };
  let executingScript;
  const context=vm.createContext({window:{},Date:Clock,AbortSignal,AbortController,
    document:{getElementById:id=>elements[id] ||= node(),createElement:node,createElementNS:(_,tag)=>node(tag)},
    setInterval:(fn,ms)=>timers.push({fn,ms,script:executingScript}),
    fetch:async url=>{calls.push(url);return {ok:true,json:async()=>data[url.split('?')[0]]};}
  });
  for (const [, , file] of scripts) {
    executingScript=file;
    vm.runInContext(fs.readFileSync('app/static/'+file,'utf8'), context, {filename:file});
  }
  await flush();
  return {calls,timers,state,elements,
    poll:async()=>{for(const timer of timers)timer.fn();await flush();}};
}
const initialRequests=['/api/connection','/api/market/live','/api/market/structure',
  '/api/candles/NIFTY?interval=5m&limit=100','/api/candles/BANKNIFTY?interval=5m&limit=100',
  '/api/options/nifty','/api/options/banknifty','/api/signals/current',
  '/api/trades/active','/api/trades/setup/nifty','/api/trades/setup/banknifty'].sort();
const recurringRequests=initialRequests.map(url=>url
  .replace('/api/candles/NIFTY?interval=5m&limit=100','/api/candles/NIFTY?interval=5m&limit=3')
  .replace('/api/candles/BANKNIFTY?interval=5m&limit=100','/api/candles/BANKNIFTY?interval=5m&limit=3')).sort();

test('the shared helper is loaded before every consumer as ordered deferred scripts',()=>{
  assert.deepEqual(scripts.map(([, , file])=>file),
    ['market_hours.js','dashboard.js','charts.js','structure.js','options.js','signals.js','trades.js']);
  for(const [, before, , after] of scripts) {
    assert.match(before+after,/\bdefer\b/);
    assert.doesNotMatch(before+after,/\basync\b/);
  }
});

for(const now of ['2026-09-28T08:59:59+05:30','2026-09-28T15:40:00+05:30',
  '2026-10-03T10:00:00+05:30','2026-10-04T10:00:00+05:30']) {
  test('all sections load once and ordinary polling stays idle at '+now,async()=>{
    const h=await page(now);
    assert.deepEqual([...h.calls].sort(),initialRequests);
    h.calls.length=0;await h.poll();await h.poll();assert.deepEqual(h.calls,[]);
    assert.deepEqual(h.timers.map(t=>[t.script,t.ms]),[
      ['dashboard.js',2000],['charts.js',5000],['structure.js',5000],['options.js',5000],
      ['options.js',5000],['signals.js',5000],['trades.js',2000]]);
  });
}

test('all polls resume at 09:00, stop at 15:40 and resume on the next weekday without reload',async()=>{
  const h=await page('2026-09-28T08:59:59+05:30');
  for(const [now,allowed] of [
    ['2026-09-28T09:00:00+05:30',true],['2026-09-28T15:39:59+05:30',true],
    ['2026-09-28T15:40:00+05:30',false],['2026-10-03T10:00:00+05:30',false],
    ['2026-10-05T09:00:00+05:30',true]]) {
    h.state.now=now;h.calls.length=0;await h.poll();
    assert.deepEqual([...h.calls].sort(),allowed?recurringRequests:[],now);
  }
});

test('dashboard Refresh and both expiry selectors fetch immediately outside hours',async()=>{
  const h=await page('2026-10-03T10:00:00+05:30');h.calls.length=0;
  await h.elements.refresh.listeners.click();
  for(const [index,expiry] of [['nifty','2026-10-06'],['banknifty','2026-10-27']]) {
    const selector=h.elements[index+'-options-expiry'];selector.value=expiry;
    await selector.listeners.change();
  }
  assert.deepEqual(h.calls,['/api/connection','/api/market/live',
    '/api/options/nifty?expiry=2026-10-06','/api/options/banknifty?expiry=2026-10-27']);
  h.calls.length=0;await h.poll();assert.deepEqual(h.calls,[]);
});

test('dashboard REST fallback runs on initial/manual requests, never an unguarded recurring timer',async()=>{
  const h=await page('2026-09-28T15:40:00+05:30',true);
  assert.equal(h.calls.filter(url=>url==='/api/market/snapshot').length,1);
  h.calls.length=0;h.state.now='2026-09-28T15:41:00+05:30';
  await h.poll();assert.deepEqual(h.calls,[]);
  await h.elements.refresh.listeners.click();
  assert.deepEqual(h.calls,['/api/connection','/api/market/live','/api/market/snapshot']);
});
