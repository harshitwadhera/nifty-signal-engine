const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tag='div') {
  return {tag,textContent:'',className:'',children:[],
    append(...items){this.children.push(...items);},
    replaceChildren(...items){this.children=items;}};
}
const text=n=>[n.textContent,...n.children.map(text)].join(' ');
const flush=()=>new Promise(resolve=>setImmediate(resolve));

async function render(data, ok=true) {
  const elements={},calls=[];
  const context=vm.createContext({
    document:{getElementById:id=>elements[id] ||= node(),createElement:tag=>node(tag)},
    window:{marketAutoRefreshAllowed:()=>true},AbortSignal,Date,setInterval(){},
    fetch:async url=>{calls.push(url);return {ok,json:async()=>data};}
  });
  vm.runInContext(fs.readFileSync('app/static/opportunities.js','utf8'),context);
  await flush();
  return {elements,calls};
}

test('confirmed opportunity table shows model result and taken flag',async()=>{
  const item={signal_id:'sig',index_name:'NIFTY',direction:'CALL',
    confirmed_at:'2026-09-30T11:10:00+05:30',state:'STOPPED',confirmation_price:25010,taken:false,
    history:[{state:'CONFIRMED'},{state:'TARGET1_HIT'},{state:'STOPPED'}],
    outcome:{entry_underlying:25010},
    plan:{option:{trading_symbol:'NIFTYTESTCE'},entry_trigger:{level:25000},
      invalidation:{level:24950},target1:{level:25100},target2:{level:25200}}};
  const {elements,calls}=await render({items:[item],total:1});
  const content=text(elements['opportunity-history']);
  for(const value of ['NIFTY','CALL','NIFTYTESTCE','25,010','24,950','25,100','25,200','T1 HIT → STOPPED','NO']) {
    assert.ok(content.includes(value),value);
  }
  assert.equal(elements['opportunity-history-status'].textContent,'Showing 1 of 1 confirmed opportunities');
  assert.deepEqual(calls,['/api/signals/opportunities?limit=25']);
});

test('taken is independent of the model outcome',async()=>{
  const item={index_name:'BANKNIFTY',direction:'PUT',confirmed_at:'2026-09-30T12:00:00+05:30',
    state:'CONFIRMED',taken:true,history:[{state:'CONFIRMED'}],
    plan:{option:{trading_symbol:'BANKTESTPE'}}};
  const {elements}=await render({items:[item],total:1});
  const content=text(elements['opportunity-history']);
  assert.ok(content.includes('CONFIRMED / ACTIVE'));
  assert.ok(content.includes('YES'));
});

test('empty history has an explicit message',async()=>{
  const {elements}=await render({items:[],total:0});
  assert.match(text(elements['opportunity-history']),/No confirmed trade opportunities/);
});

test('history failure clears stale rows and shows an unavailable message',async()=>{
  const {elements}=await render({items:[{index_name:'NIFTY'}],total:1},false);
  assert.match(elements['opportunity-history-status'].textContent,/History unavailable/);
  const content=text(elements['opportunity-history']);
  assert.match(content,/temporarily unavailable/);
  assert.ok(!content.includes('NIFTY'));
});
