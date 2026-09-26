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
  const elements={},body=node('body'),calls=[],timers=new Map(); let sounds=0,cancelled=0,clock=0,nextTimer=1;
  const data={setup:initialSetup,trade:initialTrade,fail:false};
  class Audio {constructor(){this.state='running';this.currentTime=0;this.destination={};}async resume(){}
    createOscillator(){return {connect(){},disconnect(){},frequency:{},start(){sounds++;},stop(at){if(at===undefined)cancelled++;}};}
    createGain(){return {connect(){},disconnect(){},gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}}};}}
  const context=vm.createContext({document:{body,getElementById:id=>elements[id] ||= node(),createElement:node},
    window:{AudioContext:Audio},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    AbortSignal,Date,
    setInterval:(fn,ms)=>{const id=nextTimer++;timers.set(id,{fn,ms,at:clock+ms,repeat:true});return id;},
    clearInterval:id=>timers.delete(id),
    setTimeout:(fn,ms)=>{const id=nextTimer++;timers.set(id,{fn,ms,at:clock+ms,repeat:false});return id;},
    clearTimeout:id=>timers.delete(id),fetch:async(url,options={})=>{
      calls.push({url,options}); if(data.fail)throw Error('offline');
      if(data.postWait && options.method==='POST')await data.postWait;
      if(url.endsWith('/acknowledge') && data.trade) {
        const event=data.trade.events.find(e=>e.event_id===JSON.parse(options.body).event_id);
        if(event)event.acknowledged_at='persisted';
      }
      if(url.endsWith('/close'))data.trade=null;
      const payload=url.endsWith('/active')?{items:data.trade?[data.trade]:[]}:url.includes('/setup/')?data.setup:{};
      return {ok:true,json:async()=>payload};
    }});
  vm.runInContext(fs.readFileSync('app/static/trades.js','utf8'),context); await flush();
  return {data,elements,body,calls,sounds:()=>sounds,cancelled:()=>cancelled,
    loops:()=>[...timers.values()].filter(t=>t.ms===1500).length,
    advance(ms){const end=clock+ms;while(true){
      const next=[...timers.entries()].filter(([id,t])=>t.ms!==2000 && t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!next)break;const [id,t]=next;clock=t.at;
      if(t.repeat)t.at+=t.ms;else timers.delete(id);t.fn();
    }clock=end;},
    refresh:async()=>{await [...timers.values()].find(t=>t.ms===2000).fn();await flush();},storage};
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
function stopEvent(ack=null){return {event_id:'STOP',kind:'STOP_HIT',at:'2026-09-25T10:10:00+05:30',underlying:24790,acknowledged_at:ack};}
async function armedTrade(){
  const t=trade(),h=await harness(setup(),t);
  await button(h.elements['nifty-trade'],'TEST ALARM').listeners.click();
  h.advance(4500);assert.equal(h.loops(),0);return {t,h};
}
test('STOP_HIT repeats every 1.5 seconds without duplicate loops on refresh',async()=>{
  const {t,h}=await armedTrade();const before=h.sounds();
  t.status='STOP_HIT';t.events=[stopEvent()];await h.refresh();
  assert.equal(h.sounds(),before+1);assert.equal(h.loops(),1);
  await h.refresh();await h.refresh();assert.equal(h.loops(),1);assert.equal(h.sounds(),before+1);
  h.advance(3000);assert.equal(h.sounds(),before+3);
  assert.ok(find(h.elements['nifty-trade'],n=>n.className==='trade-stop'));
});
test('ACKNOWLEDGE immediately cancels timer and audio, persists, and leaves banner/trade open',async()=>{
  const {t,h}=await armedTrade();t.events=[stopEvent()];await h.refresh();
  let release;h.data.postWait=new Promise(resolve=>release=resolve);
  const pending=button(h.elements['nifty-trade'],'ACKNOWLEDGE').listeners.click();
  assert.equal(h.loops(),0);assert.ok(h.cancelled()>0);
  const before=h.sounds();await h.refresh();h.advance(3000);assert.equal(h.sounds(),before);
  release();await pending;h.data.postWait=null;
  assert.equal(t.events[0].acknowledged_at,'persisted');assert.equal(h.data.trade,t);
  assert.match(text(h.elements['nifty-trade']),/Acknowledged .* trade remains open/);
  assert.ok(find(h.elements['nifty-trade'],n=>n.className==='trade-stop'));
  await h.refresh();assert.equal(h.loops(),0);
});
test('confirmed user exit stops immediately and removes trade; opening/cancelling dialog does not silence',async()=>{
  const {t,h}=await armedTrade();t.events=[stopEvent()];await h.refresh();
  button(h.elements['nifty-trade'],'I EXITED THE TRADE').listeners.click();assert.equal(h.loops(),1);
  const form=find(h.body,n=>n.tag==='form');
  let release;h.data.postWait=new Promise(resolve=>release=resolve);
  const pending=form.listeners.submit({preventDefault(){}});assert.equal(h.loops(),0);
  release();await pending;h.data.postWait=null;
  assert.equal(h.data.trade,null);await h.refresh();assert.equal(h.loops(),0);
});
test('acknowledged STOP never restarts after reload; unacknowledged STOP needs user audio permission',async()=>{
  const t=trade();t.events=[stopEvent('persisted')];
  const h=await harness(setup(),t);assert.equal(h.sounds(),0);
  await button(h.elements['nifty-trade'],'TEST ALARM').listeners.click();h.advance(4500);
  const count=h.sounds();await h.refresh();h.advance(3000);assert.equal(h.sounds(),count);assert.equal(h.loops(),0);
  t.events=[stopEvent()];const reboot=await harness(setup(),t);
  assert.equal(reboot.loops(),0);assert.equal(reboot.sounds(),0);
  await button(reboot.elements['nifty-trade'],'TEST ALARM').listeners.click();assert.equal(reboot.loops(),1);
  reboot.advance(6000);assert.equal(reboot.loops(),1);
});
for(const kind of ['T1_HIT','T2_HIT'])test(kind+' plays one short sound, never a repeating loop',async()=>{
  const {t,h}=await armedTrade();const before=h.sounds();
  t.events=[{event_id:kind,kind,at:'now',underlying:25101}];await h.refresh();
  assert.equal(h.sounds(),before+1);assert.equal(h.loops(),0);
  await h.refresh();h.advance(5000);assert.equal(h.sounds(),before+1);
});
test('stale crossing never starts stop alarm; stale new event is deferred but existing genuine alarm stays latched',async()=>{
  const {t,h}=await armedTrade();const before=h.sounds();
  t.current_underlying=24790;t.monitoring_status='PAUSED';await h.refresh();
  assert.equal(h.loops(),0);assert.equal(h.sounds(),before);assert.match(text(h.elements['nifty-trade']),/MONITORING PAUSED/);
  t.events=[stopEvent()];await h.refresh();assert.equal(h.loops(),0);
  t.monitoring_status='LIVE';await h.refresh();assert.equal(h.loops(),1);
  t.monitoring_status='PAUSED';await h.refresh();h.advance(1500);assert.equal(h.loops(),1);
});
test('TEST ALARM runs one bounded pattern for 4.5 seconds and repeated clicks do not stack timers',async()=>{
  const h=await harness(setup(),trade());
  await button(h.elements['nifty-trade'],'TEST ALARM').listeners.click();
  await button(h.elements['nifty-trade'],'TEST ALARM').listeners.click();
  assert.equal(h.loops(),1);assert.equal(h.sounds(),1);h.advance(3000);assert.equal(h.sounds(),3);
  h.advance(1500);assert.equal(h.loops(),0);const count=h.sounds();h.advance(3000);assert.equal(h.sounds(),count);
});
test('server closure or acknowledgement from another tab clears the repeating alarm',async()=>{
  for(const action of ['close','ack']){
    const {t,h}=await armedTrade();t.events=[stopEvent()];await h.refresh();assert.equal(h.loops(),1);
    if(action==='close')h.data.trade=null;else t.events[0].acknowledged_at='other tab';
    await h.refresh();assert.equal(h.loops(),0);
  }
});
test('acknowledgement is a journal action, not a close; fetch failure pauses display',async()=>{
  const t=trade();t.events=[{event_id:'stop',kind:'STOP_HIT',at:'now',underlying:24790}];
  const h=await harness(setup(),t);await button(h.elements['nifty-trade'],'ACKNOWLEDGE').listeners.click();
  assert.equal(JSON.parse(h.calls.find(c=>c.url.endsWith('/acknowledge')).options.body).event_id,'stop');
  assert.ok(!h.calls.some(c=>c.url.endsWith('/close')));
  h.data.fail=true;await h.refresh();assert.match(text(h.elements['nifty-trade']),/MONITORING PAUSED/);
});
test('invalid structural range never displays READY or entry button and prominently explains why',async()=>{
  const reason='Underlying has already reached the structural stop or first target; this setup is no longer actionable.';
  for(const state of ['READY','NO_TRADE']) {
    const s={...setup(state),can_confirm:false,reason};
    const h=await harness(s),panel=h.elements['nifty-trade'];
    assert.equal(button(panel,'I TOOK THIS TRADE'),undefined);
    assert.ok(!text(panel).includes('READY'));
    assert.match(text(panel),/NO TRADE/);
    assert.equal(find(panel,n=>n.className==='trade-paused').textContent,reason);
  }
});
