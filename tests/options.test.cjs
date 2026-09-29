const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function node() { return {textContent:'', value:'', children:[], listeners:{},
  append(...items) {this.children.push(...items);}, replaceChildren(...items) {this.children=items;},
  addEventListener(event,action) {this.listeners[event]=action;}}; }
async function render(data, ok=true, request=null) {
  const elements={}, calls=[];
  const context=vm.createContext({document:{getElementById:id=>elements[id] ||= node(),createElement:node},
    AbortSignal, Date, encodeURIComponent, setInterval(){},
    fetch:async url=>{calls.push(url);return request ? request(url) : {ok,json:async()=>data};}});
  vm.runInContext(fs.readFileSync('app/static/options.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {elements,calls};
}
test('options shows distinct baselines, local Greeks and full-chain coverage',async()=>{
  const {elements}=await render({stale:false,selected_expiry:'2026-09-28',expiries:['2026-09-28'],
    coverage:{expected_contracts:40,received_contracts:40,percent:100},atm_ce:{iv:.2,delta:.5,oi_change_session:10,oi_change_vs_previous_close:30},atm_pe:null});
  const panel=elements['nifty-options'];
  assert.equal(panel.children[0].textContent,'FRESH SNAPSHOT');
  const fields=panel.children[1].children.map(n=>n.textContent);
  assert.ok(fields.includes('Full-expiry OI PCR'));
  assert.ok(fields.includes('ATM CE ΔOI session'));
  assert.ok(fields.includes('ATM CE ΔOI vs previous close'));
  assert.ok(fields.includes('20'));
  assert.ok(fields.includes('Unavailable'));
  assert.match(panel.children[2].textContent,/40\/40/);
});
test('incomplete options visibly marked stale',async()=>{
  const {elements}=await render({stale:true,coverage:{expected_contracts:100,received_contracts:20,percent:20}});
  assert.match(elements['banknifty-options'].children[0].textContent,/INCOMPLETE/);
});

test('analysis shows manual versus automatic expiry context and existing writing/unwinding metrics',async()=>{
  const {elements,calls}=await render({selected_expiry:'2026-09-29',expiry_selection:{analysis_expiry:'2026-10-06',
    nearest:'2026-09-29',monthly:'2026-09-29',analysis_expiry_policy:{label:'Next-week expiry',reason:'Current-week expiry skipped'}},
    call_writing_zones:[{strike:25000,oi_change_session:1234}],put_writing_zones:[{strike:24800,oi_change_session:5678}],
    call_unwinding_zones:[{strike:25200,value:-900}],put_unwinding_zones:[{strike:24600,value:-800}]});
  const fields=elements['nifty-options'].children[1].children.map(n=>n.textContent);
  for(const value of ['2026-09-29','2026-10-06','Automatic analysis expiry','Next-week expiry','Current-week expiry skipped',
    'Call writing zones (session)','Put writing zones (session)','25,000 (1,234)','24,800 (5,678)',
    'Call unwinding zones (session)','Put unwinding zones (session)','25,200 (-900)','24,600 (-800)']) assert.ok(fields.includes(value),value);
  assert.deepEqual(calls,['/api/options/nifty','/api/options/banknifty']);
});
test('expiry selection requests selected expiry',async()=>{
  const {elements,calls}=await render({stale:true,expiries:['2026-09-28']});
  elements['nifty-options-expiry'].value='2026-09-28';
  elements['nifty-options-expiry'].listeners.change();
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(calls.includes('/api/options/nifty?expiry=2026-09-28'));
  assert.equal(elements['nifty-options-expiry'].children[0].textContent,'Automatic analysis expiry');
  elements['nifty-options-expiry'].value='';
  elements['nifty-options-expiry'].listeners.change();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.at(-1),'/api/options/nifty');
});
test('options errors clear data instead of leaving fresh values',async()=>{
  const {elements}=await render({},false);
  assert.match(elements['nifty-options'].textContent,/unavailable/);
  assert.equal(elements['nifty-options'].children.length,0);
});

test('expired manual selection returns to automatic inspection instead of retrying a stale expiry forever',async()=>{
  const data={stale:true,expiries:['2026-10-06']};
  const {elements,calls}=await render(data,true,url=>({ok:!url.includes('?expiry='),
    status:url.includes('?expiry=') ? 404 : 200,json:async()=>data}));
  elements['nifty-options-expiry'].value='2026-09-29';
  elements['nifty-options-expiry'].listeners.change();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(elements['nifty-options-expiry'].value,'');
  assert.deepEqual(calls.slice(-2),['/api/options/nifty?expiry=2026-09-29','/api/options/nifty']);
});
