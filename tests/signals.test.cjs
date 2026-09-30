const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function node() { return {textContent:'', className:'', children:[], listeners:{}, attrs:{},
  append(...v){this.children.push(...v);}, replaceChildren(...v){this.children=v;},
  addEventListener(event,action){this.listeners[event]=action;}, setAttribute(key,value){this.attrs[key]=value;}}; }
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

test('both indices display backend expiry policy, weightage and independent qualification gates',async()=>{
  const {elements}=await render({decision:'NO_TRADE',bullish_score:0,bearish_score:31.5,
    expiry_selection:{analysis_expiry:'2026-10-06',nearest:'2026-09-29',analysis_expiry_policy:{
      code:'NIFTY_NEXT_WEEK_EXPIRY',label:'Next-week expiry',reason:'Current-week expiry skipped for Monday/Tuesday analysis'}},
    category_scores:{options_positioning:{direction:'unavailable',bullish_points:0,bearish_points:0,available_weight:0,maximum_weight:30},
      breadth_constituents:{direction:'bearish',bullish_points:0,bearish_points:9,available_weight:12,maximum_weight:15}},
    qualification_gates:[{key:'minimum_score',label:'Winning score',status:'BLOCK',passed:false,actual:31.5,required:60},
      {key:'option_coverage',label:'Overall options chain coverage (%)',status:'INFO',passed:null,actual:97.22,detail:'Diagnostic only'},
      {key:'options_signal_data',label:'Options signal data',status:'BLOCK',passed:false,detail:'No current usable component evidence'},
      {key:'entry_window',label:'New-entry window',status:'BLOCK',passed:false,actual:'Closed',required:'09:15–15:00 IST'},
      {key:'planning',label:'Signal planning',status:'BLOCK',passed:false,detail:'Outside new-entry window'}],
    evidence:['Evidence item'],contradictions:['Contradiction item'],data_quality:{stale:true}});
  for (const index of ['nifty','banknifty']) {
    const content=text(elements[index+'-signal']);
    for (const label of ['Analysis expiry: 06 Oct 2026','Next-week expiry','Current-week expiry skipped',
      'Current category weightage','Available / Max','0 / 30','12 / 15','TOTAL','WHY NO TRADE',
      'BLOCK Winning score 31.5 / 60 required','INFO Overall options chain coverage (%) 97.22',
      'BLOCK Options signal data','No current usable component evidence','Closed / 09:15–15:00 IST required',
      'Outside new-entry window','Evidence','Contradictions','Data quality']) assert.ok(content.includes(label), label);
  }
});

test('UI takes configured maxima, statuses and policy from backend without recomputing',async()=>{
  const {elements}=await render({decision:'PUT',expiry_selection:{analysis_expiry:'2026-10-27',
    analysis_expiry_policy:{label:'Next monthly expiry',reason:'Current monthly expiry is in its final expiry window'}},
    category_scores:{price_trend:{direction:'bearish',bullish_points:0,bearish_points:10,available_weight:20,maximum_weight:25}},
    qualification_gates:[{label:'Backend gate',passed:true,status:'PASS',actual:1,required:999},
      {label:'Planning',status:'INFO',detail:'Existing signal lifecycle observed; no new signal'}]});
  const content=text(elements['banknifty-signal']);
  for (const value of ['27 Oct 2026','Next monthly expiry','final expiry window','20 / 25','PASS Backend gate 1 / 999 required',
    'INFO Planning','CURRENT QUALIFICATION']) assert.ok(content.includes(value), value);
});

test('current gates and table use current scores while active creation evidence remains accessible',async()=>{
  const {elements}=await render({decision:'CALL',score_basis:'candidate_creation',bullish_score:90,
    score_expiry_selection:{analysis_expiry:'2026-09-29'},
    category_scores:{options_positioning:{direction:'bullish',bullish_points:30,bearish_points:0,available_weight:30,maximum_weight:30}},
    current_qualification:{bullish_score:20,bearish_score:0,category_scores:{options_positioning:{direction:'unavailable',
      bullish_points:0,bearish_points:0,available_weight:0,maximum_weight:30}}},
    qualification_gates:[{label:'Options signal data',status:'BLOCK',detail:'Stale'}]});
  const content=text(elements['nifty-signal']);
  assert.match(content,/Options unavailable 0 0 0 \/ 30/);
  assert.match(content,/TOTAL  20 0/);
  assert.match(content,/recorded at creation \(expiry: 29 Sept? 2026\)/);
  assert.match(content,/bullish 30/);
});

test('missing analysis expiry is visibly unavailable and backend policy text stays escaped',async()=>{
  const reason='<img src=x onerror=alert(1)>';
  const {elements}=await render({decision:'NO_TRADE',expiry_selection:{analysis_expiry:null,
    analysis_expiry_policy:{label:'Next monthly expiry',reason}},qualification_gates:[]});
  const content=text(elements['nifty-signal']);
  assert.ok(content.includes('Analysis expiry: Unavailable'));
  assert.ok(content.includes(reason));
  assert.ok(content.includes('INFO Qualification Waiting for backend gate details'));
});

test('partial Options weights and current component diagnostics stay usable below 95 percent',async()=>{
  const {elements,calls}=await render({decision:'CALL',data_quality:{stale:false},
    current_qualification:{category_scores:{options_positioning:{direction:'bullish',bullish_points:16.5,
      bearish_points:0,available_weight:23.5,maximum_weight:30}},data_quality:{options_quality:{
      coverage:{expected_contracts:214,fresh_contracts:208,percent:97.2},full_chain_fresh:false,
      atm_quality:{ce:{fresh:true,liquidity:'LIQUID',reason:'Usable'},pe:{fresh:true,liquidity:'LIQUID',reason:'Usable'}},
      component_availability:{atm_behavior:{available_weight:9,maximum_weight:9,reason:'Both sides usable'},
        oi_wall_breakout:{available_weight:0,maximum_weight:4.5,reason:'Insufficient valid OI-wall data'},
        pcr_confirmation:{available_weight:4,maximum_weight:6,reason:'2 of 3 ratios usable'}}}}},
    qualification_gates:[{label:'Overall options chain coverage (%)',status:'INFO',actual:84.7,detail:'Diagnostic only'},
      {label:'Options signal data',status:'PASS',detail:'Usable component evidence'}]});
  const panel=elements['nifty-signal'], content=text(panel);
  for (const value of ['Options bullish 16.5 0 23.5 / 30','208 / 214 (97.2%)','Full chain fresh: No',
    'ATM CE: Fresh','ATM PE: Fresh','ATM behavior: 9 / 9 available','OI walls: 0 / 4.5 unavailable',
    'PCR: 4 / 6 available','INFO Overall options chain coverage (%) 84.7','PASS Options signal data']) assert.ok(content.includes(value),value);
  const details=panel.children.find(n=>n.children[0]?.textContent==='OPTIONS DATA QUALITY');
  assert.ok(details && !details.open);
  assert.ok(!content.includes('[object Object]'));
  assert.deepEqual(calls,['/api/signals/current']);
});


test('qualification details are kept behind the compact info control',async()=>{
  const {elements}=await render({decision:'NO_TRADE',qualification_gates:[
    {label:'Winning score',status:'BLOCK',actual:55,required:60,detail:'Below threshold'}]});
  const panel=elements['nifty-signal'];
  const info=panel.children.find(n=>n.className==='signal-decision-info');
  assert.ok(info);
  assert.equal(info.children[0].textContent,'ⓘ');
  assert.match(text(info),/WHY NO TRADE/);
  assert.match(text(info),/Winning score/);
  assert.equal(info.children[0].attrs['aria-expanded'],'false');
  info.children[0].listeners.click();
  assert.match(info.className,/open/);
  assert.equal(info.children[0].attrs['aria-expanded'],'true');
});
