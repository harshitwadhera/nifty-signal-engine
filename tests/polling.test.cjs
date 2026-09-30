const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {node} = require('./helpers/dom.cjs');

const flush = () => new Promise(resolve => setImmediate(resolve));
const html = name => fs.readFileSync(`app/static/${name}.html`, 'utf8');
const scripts = name => [...html(name).matchAll(/<script\b([^>]*?)src="\/static\/([^"]+)"([^>]*)><\/script>/g)];
async function page(now, missingTicks=false, name='index', trade=null) {
  const elements={}, calls=[], timers=[], missing=[], events={};
  // Only real page IDs exist. A cross-page script cannot hide a missing DOM
  // dependency by letting the test invent its expected element.
  for (const match of html(name).matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) elements[match[2]]=node(match[1]);
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
    '/api/signals/opportunities':{items:[],total:0},
    '/api/options/nifty':{expiries:['2026-10-06']},
    '/api/options/banknifty':{expiries:['2026-10-27']},
    '/api/candles/NIFTY':{symbol:'NIFTY 50',interval:'5m',candles:[{
      start_time:'2026-09-28T09:55:00+05:30',end_time:'2026-09-28T10:00:00+05:30',
      open:100,high:101,low:99,close:100.5,completed:false,partial:false,source:'live'}]},
    '/api/candles/BANKNIFTY':{symbol:'NIFTY BANK',interval:'5m',candles:[{
      start_time:'2026-09-28T09:55:00+05:30',end_time:'2026-09-28T10:00:00+05:30',
      open:100,high:101,low:99,close:100.5,completed:false,partial:false,source:'live'}]},
    '/api/trades/active':{items:trade ? [trade] : []},
    '/api/trades/setup/nifty':{setup_state:'NO_TRADE'},
    '/api/trades/setup/banknifty':{setup_state:'NO_TRADE'}
  };
  let executingScript;
  let sounds=0;
  class Audio {
    constructor(){this.state='running';this.currentTime=0;this.destination={};}
    async resume(){}
    createOscillator(){return {connect(){},disconnect(){},frequency:{},start(){sounds++;},stop(){}};}
    createGain(){return {connect(){},disconnect(){},gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}}};}
  }
  function interval(fn,ms) {const timer={fn,ms,script:executingScript,active:true};timers.push(timer);return timer;}
  const context=vm.createContext({window:{AudioContext:Audio,addEventListener:(event,fn)=>events[event]=fn},Date:Clock,AbortSignal,AbortController,
    localStorage:{getItem(){return null;},setItem(){}},
    document:{body:node('body'),getElementById:id=>{
      if(!elements[id])missing.push(id);
      return elements[id] || null;
    },createElement:node,createElementNS:(_,tag)=>node(tag)},
    setInterval:interval,clearInterval:timer=>{if(timer)timer.active=false;},
    setTimeout:interval,clearTimeout:timer=>{if(timer)timer.active=false;},
    fetch:async(url,options={})=>{
      calls.push(url);
      if(url.endsWith('/acknowledge')) {
        trade.events.find(e=>e.event_id===JSON.parse(options.body).event_id).acknowledged_at='saved';
        return {ok:true,json:async()=>({})};
      }
      assert.ok(url.split('?')[0] in data, 'Unexpected endpoint: '+url);
      return {ok:true,json:async()=>data[url.split('?')[0]]};
    }
  });
  for (const [, , file] of scripts(name)) {
    executingScript=file;
    vm.runInContext(fs.readFileSync('app/static/'+file,'utf8'), context, {filename:file});
  }
  await flush();
  assert.deepEqual(missing,[]);
  return {calls,timers,state,elements,events,sounds:()=>sounds,
    poll:async()=>{for(const timer of [...timers])if(timer.active && timer.ms>=2000)timer.fn();await flush();assert.deepEqual(missing,[]);}};
}
const initialRequests={index:['/api/connection','/api/market/live',
  '/api/candles/NIFTY?interval=5m&limit=100','/api/candles/BANKNIFTY?interval=5m&limit=100',
  '/api/signals/current','/api/signals/opportunities?limit=25','/api/trades/active','/api/trades/setup/nifty','/api/trades/setup/banknifty'].sort(),
  analysis:['/api/market/structure','/api/options/nifty','/api/options/banknifty'].sort()};
const recurringRequests=name=>initialRequests[name].map(url=>url
  .replace('/api/candles/NIFTY?interval=5m&limit=100','/api/candles/NIFTY?interval=5m&limit=3')
  .replace('/api/candles/BANKNIFTY?interval=5m&limit=100','/api/candles/BANKNIFTY?interval=5m&limit=3')).sort();

for(const [name,files] of Object.entries({index:['market_hours.js','dashboard.js','charts.js','signals.js','trades.js','opportunities.js'],
  analysis:['market_hours.js','structure.js','options.js']})) {
test(`${name} loads only its own consumers after the shared helper`,()=>{
  assert.deepEqual(scripts(name).map(([, , file])=>file),files);
  for(const [, before, , after] of scripts(name)) {
    assert.match(before+after,/\bdefer\b/);
    assert.doesNotMatch(before+after,/\basync\b/);
  }
});

for(const now of ['2026-09-28T08:59:59+05:30','2026-09-28T15:40:00+05:30',
  '2026-10-03T10:00:00+05:30','2026-10-04T10:00:00+05:30']) {
test(`${name} sections load once and ordinary polling stays idle at ${now}`,async()=>{
    const h=await page(now,false,name);
    assert.deepEqual([...h.calls].sort(),initialRequests[name]);
    h.calls.length=0;await h.poll();await h.poll();assert.deepEqual(h.calls,[]);
    assert.deepEqual(h.timers.map(t=>[t.script,t.ms]),name==='index' ? [
      ['dashboard.js',2000],['charts.js',5000],['signals.js',5000],['trades.js',2000],['opportunities.js',10000]] : [
      ['structure.js',5000],['options.js',5000],['options.js',5000]]);
  });
}

test(`${name} polls resume at 09:00, stop at 15:40 and resume next weekday without reload`,async()=>{
  const h=await page('2026-09-28T08:59:59+05:30',false,name);
  for(const [now,allowed] of [
    ['2026-09-28T09:00:00+05:30',true],['2026-09-28T15:39:59+05:30',true],
    ['2026-09-28T15:40:00+05:30',false],['2026-10-03T10:00:00+05:30',false],
    ['2026-10-05T09:00:00+05:30',true]]) {
    h.state.now=now;h.calls.length=0;await h.poll();
    assert.deepEqual([...h.calls].sort(),allowed?recurringRequests(name):[],now);
  }
});
}

test('dashboard Refresh fetches immediately outside hours without loading analysis endpoints',async()=>{
  const h=await page('2026-10-03T10:00:00+05:30');h.calls.length=0;
  await h.elements.refresh.listeners.click();
  assert.deepEqual(h.calls,['/api/connection','/api/market/live']);
  h.calls.length=0;await h.poll();assert.deepEqual(h.calls,[]);
});

test('analysis expiry selectors fetch immediately outside hours without loading trade endpoints',async()=>{
  const h=await page('2026-10-03T10:00:00+05:30',false,'analysis');h.calls.length=0;
  for(const [index,expiry] of [['nifty','2026-10-06'],['banknifty','2026-10-27']]) {
    const selector=h.elements[index+'-options-expiry'];selector.value=expiry;
    await selector.listeners.change();
  }
  assert.deepEqual(h.calls,['/api/options/nifty?expiry=2026-10-06','/api/options/banknifty?expiry=2026-10-27']);
  h.calls.length=0;await h.poll();assert.deepEqual(h.calls,[]);
});

test('trade dashboard keeps active STOP monitoring and alarm acknowledgement outside market hours',async()=>{
  const trade={trade_id:'trade',index_name:'NIFTY',status:'STOP_HIT',monitoring_status:'LIVE',events:[
    {event_id:'stop',kind:'STOP_HIT',underlying:24900,at:'2026-09-28T15:40:00+05:30'}]};
  const h=await page('2026-09-28T15:41:00+05:30',false,'index',trade);
  const find=(n,label)=>n.tagName==='button' && n.textContent===label ? n : n.children.map(c=>find(c,label)).find(Boolean);
  await find(h.elements['nifty-trade'],'TEST ALARM').listeners.click();
  assert.ok(h.sounds()>0);
  const loops=()=>h.timers.filter(t=>t.active && t.ms===1500);
  assert.equal(loops().length,1);
  h.calls.length=0;await h.poll();
  assert.deepEqual(h.calls,['/api/trades/active']);
  const sounds=h.sounds();loops()[0].fn();assert.equal(h.sounds(),sounds+1);
  await find(h.elements['nifty-trade'],'ACKNOWLEDGE').listeners.click();
  assert.equal(loops().length,0);
  assert.equal(trade.status,'STOP_HIT'); // Acknowledgement does not close the trade.
});

test('analysis has no trade UI, alarm timer or pagehide handler even while a trade exists',async()=>{
  const h=await page('2026-09-28T10:00:00+05:30',false,'analysis',{trade_id:'trade',status:'STOP_HIT'});
  assert.deepEqual([...h.calls].sort(),initialRequests.analysis);
  assert.equal(h.sounds(),0);
  assert.equal(h.elements['nifty-trade'],undefined);
  assert.equal(h.events.pagehide,undefined);
  h.calls.length=0;await h.poll();
  assert.deepEqual([...h.calls].sort(),initialRequests.analysis);
});

test('dashboard REST fallback runs on initial/manual requests, never an unguarded recurring timer',async()=>{
  const h=await page('2026-09-28T15:40:00+05:30',true);
  assert.equal(h.calls.filter(url=>url==='/api/market/snapshot').length,1);
  h.calls.length=0;h.state.now='2026-09-28T15:41:00+05:30';
  await h.poll();assert.deepEqual(h.calls,[]);
  await h.elements.refresh.listeners.click();
  assert.deepEqual(h.calls,['/api/connection','/api/market/live','/api/market/snapshot']);
});
