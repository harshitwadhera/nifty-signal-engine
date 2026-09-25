const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function node() { return {textContent:'', value:'', children:[], listeners:{},
  append(...items) {this.children.push(...items);}, replaceChildren(...items) {this.children=items;},
  addEventListener(event,action) {this.listeners[event]=action;}}; }
async function render(data, ok=true) {
  const elements={}, calls=[];
  const context=vm.createContext({document:{getElementById:id=>elements[id] ||= node(),createElement:node},
    AbortSignal, Date, encodeURIComponent, setInterval(){},
    fetch:async url=>{calls.push(url);return {ok,json:async()=>data};}});
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
test('expiry selection requests selected expiry',async()=>{
  const {elements,calls}=await render({stale:true,expiries:['2026-09-28']});
  elements['nifty-options-expiry'].value='2026-09-28';
  elements['nifty-options-expiry'].listeners.change();
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(calls.includes('/api/options/nifty?expiry=2026-09-28'));
});
test('options errors clear data instead of leaving fresh values',async()=>{
  const {elements}=await render({},false);
  assert.match(elements['nifty-options'].textContent,/unavailable/);
  assert.equal(elements['nifty-options'].children.length,0);
});
