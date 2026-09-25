(() => {
  const get = id => document.getElementById(id);
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const fmt = v => v == null ? 'Unavailable' : String(v);
  function fields(parent, rows) {
    const dl = el('dl');
    for (const [key, value] of rows) dl.append(el('dt', key), el('dd', fmt(value)));
    parent.append(dl);
  }
  function button(parent, label, action) {
    const b = el('button', label); b.type = 'button'; b.addEventListener('click', action); parent.append(b); return b;
  }
  let audio = null, armed = false, busy = false, initialized = false;
  const seen = new Set();
  async function arm() {
    try {
      const Context = window.AudioContext || window.webkitAudioContext;
      audio ||= new Context(); await audio.resume(); armed = audio.state === 'running';
    } catch { armed = false; }
    return armed;
  }
  function sound(stop=false) {
    if (!armed || audio?.state !== 'running') return;
    const osc = audio.createOscillator(), gain = audio.createGain();
    osc.connect(gain); gain.connect(audio.destination);
    osc.frequency.value = stop ? 880 : 520;
    gain.gain.setValueAtTime(.15, audio.currentTime);
    gain.gain.exponentialRampToValueAtTime(.001, audio.currentTime + (stop ? 1.2 : .3));
    osc.start(); osc.stop(audio.currentTime + (stop ? 1.3 : .4));
  }
  function alarms(events) {
    for (const event of events || []) {
      const key = 'manual-trade-alarm:'+event.event_id;
      let played = seen.has(key);
      try { played ||= localStorage.getItem(key) === 'played'; } catch {}
      if (!initialized || event.acknowledged_at || played) { seen.add(key); continue; }
      if (armed) {
        sound(event.kind === 'STOP_HIT'); seen.add(key);
        try { localStorage.setItem(key, 'played'); } catch {}
      }
    }
  }
  async function request(url, body) {
    const response = await fetch(url, {signal: AbortSignal.timeout(10000), ...(body === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || 'Request failed. Refresh and try again.');
    return result;
  }
  function dialog(title, rows, inputs, submitLabel, action) {
    const box = el('dialog'), form = el('form'), heading = el('h2', title), error = el('p');
    error.setAttribute('role', 'alert'); form.append(heading); fields(form, rows);
    const values = {};
    for (const spec of inputs) {
      const label = el('label', spec.label), input = el('input');
      input.type = spec.type || 'number'; input.name = spec.name;
      input.value = spec.value ?? ''; input.required = !spec.optional;
      if (input.type === 'number') { input.min = spec.min ?? '0.01'; input.step = spec.step || 'any'; }
      label.append(input); form.append(label); values[spec.name] = input;
    }
    if (values.lots && values.quantity) {
      const lotSize = Number(values.quantity.value);
      values.lots.addEventListener('input', () => { values.quantity.value = Number(values.lots.value)*lotSize; });
    }
    const submit = el('button', submitLabel); submit.type = 'submit'; form.append(error, submit);
    button(form, 'CANCEL', () => { box.close(); box.remove(); });
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (submit.disabled || !form.reportValidity()) return;
      submit.disabled = true;
      try { await action(values); box.close(); box.remove(); await refresh(); }
      catch (e) { error.textContent = e.message; submit.disabled = false; }
    });
    box.append(form); document.body.append(box); box.showModal();
    box.addEventListener('close', () => box.remove());
  }
  function confirm(setup) {
    arm();
    const r = setup.signal.record, p = r.plan;
    dialog('Confirm your manual trade', [['Signal ID', r.signal_id], ['Index', setup.index],
      ['Option contract', p.option.trading_symbol], ['Direction', p.direction], ['Lot size', setup.lot_size],
      ['Journal only', 'This records a trade you already placed manually. No order will be sent.']], [
      {name:'lots', label:'Lots', value:1, min:1, step:1},
      {name:'quantity', label:'Quantity', value:setup.lot_size, min:1, step:1},
      {name:'actual_entry_premium', label:'Actual option entry premium', value:setup.option_ltp},
      {name:'underlying_entry', label:'Underlying entry', value:setup.underlying_current},
      {name:'opened_at', label:'Entry timestamp (include timezone, e.g. +05:30)', type:'text', value:setup.server_time}
    ], 'CONFIRM TRADE', v => request('/api/trades/'+r.signal_id+'/confirm', {
      lots:Number(v.lots.value), quantity:Number(v.quantity.value), actual_entry_premium:Number(v.actual_entry_premium.value),
      underlying_entry:Number(v.underlying_entry.value), opened_at:v.opened_at.value}));
  }
  function close(trade) {
    dialog('I exited the trade', [['Contract', trade.option_symbol], ['Quantity', trade.quantity]], [
      {name:'premium', label:'Actual exit option premium (optional)', optional:true, min:0}
    ], 'CONFIRM EXIT', v => request('/api/trades/'+trade.trade_id+'/close', {
      actual_exit_premium:v.premium.value === '' ? null : Number(v.premium.value)}));
  }
  function paint(index, setup, trade) {
    const box = get(index+'-trade'); box.replaceChildren(); box.className = 'trade-box';
    if (trade) {
      box.append(el('h3', 'ACTIVE TRADE'));
      if (trade.monitoring_status !== 'LIVE') box.append(el('p', '⚠ MONITORING PAUSED — DATA STALE', 'trade-paused'));
      fields(box, [['Contract', trade.option_symbol], ['Quantity', trade.quantity], ['Actual option entry', trade.actual_entry_premium],
        ['Current option LTP', trade.current_option_ltp], ['Current underlying', trade.current_underlying],
        ['UNDERLYING STOP / INVALIDATION', trade.underlying_stop], ['Distance to stop', trade.distance_to_stop],
        ['T1', trade.target1], ['T2', trade.target2], ['Trade status', trade.status],
        ['Data freshness', trade.monitoring_status], ['Opened at', trade.opened_at]]);
      for (const event of trade.events || []) {
        const banner = el('div', event.kind === 'STOP_HIT' ? 'EXIT TRADE — UNDERLYING STOP HIT' :
          event.kind === 'T1_HIT' ? 'TARGET 1 HIT' : 'TARGET 2 HIT', event.kind === 'STOP_HIT' ? 'trade-stop' : 'trade-target');
        fields(banner, [['Observed underlying', event.underlying], ['Timestamp', event.at]]);
        if (event.acknowledged_at) banner.append(el('p', 'Acknowledged — trade remains open'));
        else button(banner, 'ACKNOWLEDGE', async () => {
          try { await request('/api/trades/'+trade.trade_id+'/acknowledge', {event_id:event.event_id}); await refresh(); }
          catch (e) { banner.append(el('p', e.message)); }
        });
        box.append(banner);
      }
      const audioStatus = el('p', armed ? 'Sound armed for this tab' : 'Sound not armed — use TEST ALARM'); box.append(audioStatus);
      button(box, 'TEST ALARM', async () => { await arm(); sound(true); audioStatus.textContent = armed ? 'Test sound played. Verify you heard it.' : 'Sound blocked by browser. Visual alerts remain available.'; });
      button(box, 'I EXITED THE TRADE', () => close(trade));
      alarms(trade.events);
    } else {
      const state = setup?.setup_state || 'NO_TRADE';
      box.append(el('h3', state === 'NO_TRADE' ? 'NO TRADE' : state === 'WAITING' ? 'WAIT FOR TRIGGER' : 'READY'));
      if (state !== 'NO_TRADE') {
        const signal = setup.signal, r = signal.record, p = r.plan;
        fields(box, [['Direction', p.direction], ['Selected option', p.option.trading_symbol], ['Expiry', p.option.expiry],
          ['Option LTP', setup.option_ltp], ['Lot size', setup.lot_size], ['Suggested quantity (1 lot)', setup.lot_size],
          ['Underlying current', setup.underlying_current], ['Underlying confirmation price', setup.index_levels ? r.confirmation_price : null],
          ['Trigger', `${p.entry_trigger.type} ${p.entry_trigger.level} ${p.entry_trigger.confirmation} (${p.entry_trigger.instrument})`],
          ['UNDERLYING STOP / INVALIDATION', setup.index_levels ? p.invalidation.level : null],
          ['T1', setup.index_levels ? p.target1.level : null], ['T2', setup.index_levels ? p.target2?.level : null],
          ['T1 R:R', r.confirmed_t1_rr ?? p.t1_rr], ['T2 R:R', r.confirmed_t2_rr ?? p.t2_rr],
          ['Confidence', signal.confidence], ['Liquidity at selection', `spread ${p.option.spread_percent}% · OI ${p.option.oi} · volume ${p.option.volume}`],
          ['Freshness', setup.fresh ? 'Fresh underlying observation' : 'STALE']]);
        if (state === 'WAITING') box.append(el('p', 'No active stop monitoring until you confirm a manual trade.'));
        if (setup.can_confirm) button(box, 'I TOOK THIS TRADE', () => confirm(setup));
      }
      if (setup?.reason) box.append(el('p', setup.reason));
    }
    box.append(el('p', 'This stop is based on NIFTY/BANKNIFTY, not option premium.', 'note'));
  }
  let cached = [];
  async function refresh() {
    if (busy) return; busy = true;
    try {
      const active = await request('/api/trades/active'); cached = active.items;
      for (const index of ['nifty', 'banknifty']) {
        const trade = cached.find(t => t.index_name === index.toUpperCase());
        let setup;
        if (!trade) {
          try { setup = await request('/api/trades/setup/'+index); }
          catch { setup = {setup_state:'NO_TRADE', reason:'Trade setup unavailable; retrying.'}; }
        }
        paint(index, setup, trade);
      }
      initialized = true;
    } catch {
      for (const index of ['nifty', 'banknifty']) {
        const old = cached.find(t => t.index_name === index.toUpperCase());
        paint(index, {setup_state:'NO_TRADE', reason:'Trade journal unavailable; monitoring cannot be verified.'},
          old ? {...old, monitoring_status:'PAUSED', current_underlying:null, current_option_ltp:null, distance_to_stop:null} : null);
      }
    } finally { busy = false; }
  }
  refresh(); setInterval(refresh, 2000);
})();
