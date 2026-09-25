const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function node() { return {textContent:'', className:'', children:[], append(...v){this.children.push(...v);}, replaceChildren(...v){this.children=v;}}; }
function text(n) {return [n.textContent, ...n.children.map(text)].join(' ');}
async function render(signal, ok=true) {
  const elements={}, calls=[];
  const context=vm.createContext({document:{getElementById:id=>elements[id] ||= node(), createElement:node},
    AbortSignal, Date, setInterval(){}, fetch:async url=>{
      calls.push(url); return {ok,json:async()=>({signals:['NIFTY','BANKNIFTY'].map(index=>({...signal,index}))})};
    }});
  vm.runInContext(fs.readFileSync('app/static/signals.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {elements,calls};
}
test('NO TRADE is a normal neutral result with reasons',async()=>{
  const {elements,calls}=await render({decision:'NO_TRADE',confidence:0,data_quality:{stale:false,blocking_reasons:['Winning score below minimum']}});
  const panel=elements['nifty-signal'];
  assert.equal(panel.children[0].textContent,'NO TRADE');
  assert.equal(panel.children[0].className,'signal-neutral');
  assert.match(text(panel),/Winning score below minimum/);
  assert.match(text(panel),/No active signal/);
  assert.deepEqual(calls,['/api/signals/current']);
});
test('directional panel shows plan, categories, evidence and outcomes',async()=>{
  const {elements}=await render({decision:'CALL',state:'CONFIRMED',confidence:85,bullish_score:85,bearish_score:5,
    last_updated:'2026-09-25T10:05:00+05:30',evidence:['Opening range breakout'],contradictions:['Minor PCR opposition'],
    category_scores:{price_trend:{direction:'bullish',bullish_points:30,bearish_points:0,available_weight:30}},
    data_quality:{stale:false,option_coverage_percent:100},record:{confirmed_t1_rr:2,
      plan:{entry_trigger:{type:'breakout',level:23000,confirmation:'5m_close_above',instrument:'NIFTY 50'},
        invalidation:{level:22950},target1:{level:23100},target2:{level:23200},t1_rr:2,t2_rr:4,
        option:{trading_symbol:'NIFTY23000CE',strike:23000,option_type:'CE',expiry:'2026-09-28',spread_percent:.5,oi:2000,volume:1000}},
      outcome:{entry_time:'2026-09-25T10:05:00+05:30',entry_underlying:23000,mfe:50,mae:5,result_r:null,duration_seconds:10}}});
  const content=text(elements['nifty-signal']);
  for (const value of ['CALL','CONFIRMED','5m_close_above','NIFTY23000CE','Liquidity at selection','Category breakdown','price trend','Opening range breakout','Minor PCR opposition','MFE / MAE','option coverage percent']) assert.ok(content.includes(value), value);
});
test('failed request clears actionable signal display',async()=>{
  const {elements}=await render({},false);
  assert.equal(elements['nifty-signal'].children[0].textContent,'NO TRADE');
  assert.match(text(elements['banknifty-signal']),/observations unavailable/);
  assert.match(text(elements['nifty-signal']),/No active signal/);
});
test('evidence is rendered as text, never executable markup',async()=>{
  const malicious='<img src=x onerror=alert(1)>';
  const {elements}=await render({decision:'NO_TRADE',evidence:[malicious],data_quality:{stale:true}});
  assert.ok(text(elements['nifty-signal']).includes(malicious));
  assert.ok(!fs.readFileSync('app/static/signals.js','utf8').includes('innerHTML'));
});
