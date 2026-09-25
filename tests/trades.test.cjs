const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function node(tag='div') {return {tag,textContent:'',children:[],listeners:{},dataset:{},
  append(...v){this.children.push(...v);},replaceChildren(...v){this.children=v;},
  addEventListener(k,v){this.listeners[k]=v;},setAttribute(){},showModal(){this.open=true;},
  close(){this.open=false;},remove(){this.removed=true;},reportValidity(){return true;}};}
const text=n=>[n.textContent,...n.children.map(text)].join(' ');
const find=(n,p)=>p(n)?n:n.children.map(c=>find(c,p)).find(Boolean);
const button=(n,label)=>find(n,x=>x.tag==='button' && x.textContent===label);
const flush=()=>new Promise(r=>setImmediate(r));
const plan={direction:'CALL',option:{trading_symbol:'NIFTYTEST',expiry:'2026-09-29',spread_percent:.2,oi:10000,volume:1000},
  entry_trigger:{type:'breakout',level:24900,confirmation:'5m_close_above',instrument:'NIFTY 50'},
  invalidation:{level:24800},target1:{level:25000},target2:{level:25100},t1_rr:1.5,t2_rr:2};
function setup(state='READY'){return {setup_state:state,can_confirm:state==='READY',index:'NIFTY',fresh:true,index_levels:true,
  lot_size:65,option_ltp:101,underlying_current:24900,server_time:'2026-09-25T10:05:00+05:30',
  signal:{record:{signal_id:'sig',plan,confirmation_price:24900},confidence:85}};}
function trade(){return {trade_id:'trade',index_name:'NIFTY',option_symbol:'NIFTYTEST',quantity:65,
  actual_entry_premium:102,underlying_stop:24800,target1:25000,target2:25100,status:'ACTIVE',monitoring_status:'LIVE',events:[]};}
async function harness(initialSetup=setup(),initialTrade=null,storage=new Map()) {
  const elements={},body=node('body'),calls=[],intervals=[]; let sounds=0;
  const data={setup:initialSetup,trade:initialTrade,fail:false};
  class Audio {constructor(){this.state='running';this.currentTime=0;this.destination={};}async resume(){}
    createOscillator(){return {connect(){},frequency:{},start(){sounds++;},stop(){}};}
    createGain(){return {connect(){},gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}}};}}
  const context=vm.createContext({document:{body,getElementById:id=>elements[id] ||= node(),createElement:node},
    window:{AudioContext:Audio},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    AbortSignal,Date,setInterval:fn=>intervals.push(fn),fetch:async(url,options={})=>{
      calls.push({url,options}); if(data.fail)throw Error('offline');
      const payload=url.endsWith('/active')?{items:data.trade?[data.trade]:[]}:url.includes('/setup/')?data.setup:{};
      return {ok:true,json:async()=>payload};
    }});
  vm.runInContext(fs.readFileSync('app/static/trades.js','utf8'),context); await flush();
  return {data,elements,body,calls,sounds:()=>sounds,refresh:async()=>{await intervals[0]();await flush();},storage};
}
test('NO TRADE and WAIT FOR TRIGGER have no entry button',async()=>{
  for(const state of ['NO_TRADE','WAITING']){
    const h=await harness(setup(state)), panel=h.elements['nifty-trade'];
    assert.match(text(panel),state==='WAITING'?/WAIT FOR TRIGGER/:/NO TRADE/);
    assert.equal(button(panel,'I TOOK THIS TRADE'),undefined);
    assert.equal(h.sounds(),0);
  }
});
test('READY dialog defaults actual lot size, allows premium edit and sends only on confirm',async()=>{
  const h=await harness(),panel=h.elements['nifty-trade'];
  assert.match(text(panel),/READY/); assert.match(text(panel),/UNDERLYING STOP/);
  button(panel,'I TOOK THIS TRADE').listeners.click(); await flush();
  const form=find(h.body,n=>n.tag==='form');
  const input=name=>find(form,n=>n.name===name);
  assert.equal(input('quantity').value,65); assert.equal(input('actual_entry_premium').value,101);
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
  input('lots').value=2;input('lots').listeners.input();assert.equal(input('quantity').value,130);
  input('actual_entry_premium').value=104.5;
  await form.listeners.submit({preventDefault(){}});
  const post=h.calls.find(c=>c.url.endsWith('/confirm'));const payload=JSON.parse(post.options.body);
  assert.equal(payload.actual_entry_premium,104.5);assert.equal(payload.quantity,130);
  assert.equal(h.sounds(),0);
});
test('active box, stale warning and close dialog persist optional exit premium',async()=>{
  const t=trade();t.monitoring_status='PAUSED';const h=await harness(setup(),t),panel=h.elements['nifty-trade'];
  for(const s of ['ACTIVE TRADE','MONITORING PAUSED','TEST ALARM','I EXITED THE TRADE'])assert.ok(text(panel).includes(s));
  assert.ok(!text(panel).includes('EXIT TRADE — UNDERLYING STOP HIT'));
  button(panel,'I EXITED THE TRADE').listeners.click();
  const form=find(h.body,n=>n.tag==='form');find(form,n=>n.name==='premium').value='87.5';
  await form.listeners.submit({preventDefault(){}});
  assert.equal(JSON.parse(h.calls.find(c=>c.url.endsWith('/close')).options.body).actual_exit_premium,87.5);
});
test('stop and target events sound once after interaction; page load/reload never autoplay',async()=>{
  const t=trade(),h=await harness(setup(),t);
  await button(h.elements['nifty-trade'],'TEST ALARM').listeners.click();assert.equal(h.sounds(),1);
  for(const [i,kind]of ['T1_HIT','T2_HIT','STOP_HIT'].entries()){
    t.events.push({event_id:kind,kind,at:'2026-09-25T10:10:00+05:30',underlying:24790});
    await h.refresh();assert.equal(h.sounds(),i+2);await h.refresh();assert.equal(h.sounds(),i+2);
  }
  const panel=h.elements['nifty-trade'];assert.match(text(panel),/TARGET 1 HIT/);assert.match(text(panel),/TARGET 2 HIT/);
  assert.ok(find(panel,n=>n.className==='trade-stop'));
  const reboot=await harness(setup(),t,h.storage);assert.equal(reboot.sounds(),0);
  await button(reboot.elements['nifty-trade'],'TEST ALARM').listeners.click();await reboot.refresh();assert.equal(reboot.sounds(),1);
});
test('acknowledgement is a journal action, not a close; fetch failure pauses display',async()=>{
  const t=trade();t.events=[{event_id:'stop',kind:'STOP_HIT',at:'now',underlying:24790}];
  const h=await harness(setup(),t);await button(h.elements['nifty-trade'],'ACKNOWLEDGE').listeners.click();
  assert.equal(JSON.parse(h.calls.find(c=>c.url.endsWith('/acknowledge')).options.body).event_id,'stop');
  assert.ok(!h.calls.some(c=>c.url.endsWith('/close')));
  h.data.fail=true;await h.refresh();assert.match(text(h.elements['nifty-trade']),/MONITORING PAUSED/);
});
