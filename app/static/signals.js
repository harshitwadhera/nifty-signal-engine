(() => {
  const get = id => document.getElementById(id);
  const format = v => v == null ? 'Unavailable' : typeof v === 'number'
    ? v.toLocaleString('en-IN', {maximumFractionDigits: 2}) : String(v);
  const stamp = v => v ? new Date(v).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST' : 'Waiting';
  const expiryDate = v => v ? new Date(v + 'T00:00:00+05:30').toLocaleDateString('en-GB',
    {day: '2-digit', month: 'short', year: 'numeric', timeZone: 'Asia/Kolkata'}) : 'Unavailable';
  const categoryLabels = {price_trend: 'Price / Trend', options_positioning: 'Options',
    breadth_constituents: 'Breadth', volatility: 'VIX', futures_structure: 'Futures'};
  const cards = new Map();
  function table(parent, caption, headings, rows) {
    const wrapper = document.createElement('div'), grid = document.createElement('table');
    wrapper.className = 'signal-table';
    const title = document.createElement('caption'); title.textContent = caption; grid.append(title);
    const head = document.createElement('thead'), header = document.createElement('tr');
    for (const label of headings) {
      const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; header.append(cell);
    }
    head.append(header); grid.append(head);
    const body = document.createElement('tbody');
    for (const values of rows) {
      const row = document.createElement('tr');
      for (const value of values) {
        const cell = document.createElement('td'); cell.textContent = format(value); row.append(cell);
      }
      body.append(row);
    }
    grid.append(body); wrapper.append(grid); parent.append(wrapper);
  }
  function qualificationControl(index) {
    const info = document.createElement('div'); info.className = 'signal-decision-info';
    const button = document.createElement('button'); button.type = 'button'; button.className = 'signal-info-button';
    button.textContent = 'ⓘ'; button.id = index + '-qualification-trigger';
    button.setAttribute('aria-label', index.toUpperCase() + ' qualification details');
    button.setAttribute('aria-controls', index + '-qualification-panel');
    const panel = document.createElement('div'); panel.className = 'signal-info-popover';
    panel.id = index + '-qualification-panel'; panel.setAttribute('role', 'region');
    panel.setAttribute('aria-labelledby', button.id);
    const close = document.createElement('button'); close.type = 'button'; close.textContent = 'Close';
    close.className = 'signal-info-close';
    const content = document.createElement('div'); content.className = 'signal-table'; content.tabIndex = 0;
    content.setAttribute('role', 'region'); content.setAttribute('aria-label', 'Qualification gates (scrollable)');
    panel.append(close, content); info.append(button, panel);
    let opened = false, pinned = false;
    function setOpen(value, restoreFocus = false) {
      opened = value;
      info.className = value ? 'signal-decision-info open' : 'signal-decision-info';
      panel.hidden = !value;
      button.setAttribute('aria-expanded', String(value));
      if (!value) pinned = false;
      if (restoreFocus) button.focus({preventScroll: true});
    }
    button.addEventListener('click', () => { setOpen(!opened || !pinned); pinned = opened; });
    close.addEventListener('click', () => setOpen(false, true));
    info.addEventListener('keydown', event => {
      if (event.key === 'Escape' && opened) { event.preventDefault(); setOpen(false, true); }
    });
    info.addEventListener('pointerenter', event => { if (event.pointerType === 'mouse') setOpen(true); });
    info.addEventListener('pointerleave', event => {
      if (event.pointerType === 'mouse' && !pinned && !info.contains(document.activeElement)) setOpen(false);
    });
    info.addEventListener('focusout', event => {
      if (!info.contains(event.relatedTarget)) setOpen(false);
    });
    setOpen(false);
    let previous = '';
    return {info, update(data) {
      button.title = data.decision === 'NO_TRADE' ? 'Why no trade' : 'Qualification details';
      const gates = data.qualification_gates || [];
      const signature = JSON.stringify([data.decision, gates]);
      if (signature === previous) return;
      previous = signature;
      const top = panel.scrollTop, left = content.scrollLeft;
      const staging = document.createElement('div');
      table(staging, data.decision === 'NO_TRADE' ? 'WHY NO TRADE' : 'CURRENT QUALIFICATION',
        ['Status', 'Gate', 'Observation / Requirement'], gates.length ? gates.map(g =>
          [g.status, g.label, [g.actual == null ? null : format(g.actual),
            g.required == null ? null : `${format(g.required)} required`].filter(Boolean).join(' / ') +
            (g.detail ? ` · ${g.detail}` : '')]) : [['INFO', 'Qualification', 'Waiting for backend gate details']]);
      content.replaceChildren(staging.firstChild.firstChild);
      panel.scrollTop = top; content.scrollLeft = left;
    }};
  }
  function qualification(parent, data) {
    const selection = data.expiry_selection || {}, policy = selection.analysis_expiry_policy || {};
    const expiry = document.createElement('p'); expiry.className = 'signal-expiry';
    expiry.textContent = `Analysis expiry: ${expiryDate(selection.analysis_expiry)}\nPolicy: ${format(policy.label)}\nReason: ${format(policy.reason)}`;
    parent.append(expiry);
    const current = data.current_qualification || data;
    const categories = Object.entries(current.category_scores || {}).map(([name, score]) =>
      [categoryLabels[name] || name, score.direction, score.bullish_points, score.bearish_points,
        `${format(score.available_weight)} / ${format(score.maximum_weight)}`]);
    categories.push(['TOTAL', '', current.bullish_score, current.bearish_score, '']);
    table(parent, 'Current category weightage', ['Category', 'Direction', 'Bull', 'Bear', 'Available / Max'], categories);
    const note = document.createElement('p'); note.className = 'note';
    note.textContent = 'Scores describe evidence on a 100-point budget. Passing a gate does not predict profitability.';
    if (data.score_basis === 'candidate_creation') note.textContent +=
      ` Tables show current observations; the active signal and scores below were recorded at creation (expiry: ${expiryDate(data.score_expiry_selection?.analysis_expiry)}).`;
    parent.append(note);
  }
  function section(parent, label, rows) {
    const details = document.createElement('details'), title = document.createElement('summary');
    title.textContent = label; details.append(title);
    const list = document.createElement('ul');
    for (const text of rows.length ? rows : ['None']) {
      const item = document.createElement('li'); item.textContent = text; list.append(item);
    }
    details.append(list); parent.append(details);
  }
  function paint(index, data) {
    let card = cards.get(index);
    if (!card) {
      const heading = document.createElement('div'), body = document.createElement('div');
      const control = qualificationControl(index);
      get(index + '-signal').replaceChildren(heading, control.info, body);
      card = {heading, body, control}; cards.set(index, card);
    }
    const box = card.body;
    box.replaceChildren(); card.heading.replaceChildren();
    const badge = document.createElement('p');
    const early = data.state === 'EARLY_SETUP';
    badge.className = early ? 'signal-early' : data.decision === 'NO_TRADE' ? 'signal-neutral' : 'signal-direction';
    badge.textContent = early ? `EARLY ${data.decision} SETUP` : (data.decision || 'NO_TRADE').replaceAll('_', ' ');
    card.heading.append(badge);
    qualification(card.heading, data); card.control.update(data);
    const record = data.record, plan = record?.plan, outcome = record?.outcome;
    const trigger = plan?.entry_trigger, option = plan?.option;
    const watch = record?.trigger_watch || {}, earlyStructure = watch.structure_1m || {};
    const entry = data.entry_diagnostics || {};
    const active = ['CANDIDATE','EARLY_SETUP','CONFIRMED','TARGET1_HIT'].includes(data.state);
    const triggerStatus = !active ? (data.state || 'NOT APPLICABLE — no active setup') :
      ['CONFIRMED','TARGET1_HIT'].includes(data.state) ? 'CONFIRMED' :
      entry.status ? entry.status.replaceAll('_', ' ') :
      watch.breached_at ? 'WAITING FOR CONFIRMATION' :
      trigger?.type === 'breakout_retest' ? 'WAITING FOR RETEST' : 'WAITING FOR TRIGGER';
    const rows = [
      ['State', data.state || 'No active signal'], ['Confidence (score / 100)', data.confidence],
      ['Entry status', triggerStatus], ['Entry explanation', entry.reason || null],
      ['Freshness', data.data_quality?.stale ? 'STALE / waiting for usable data' : 'Current observation'],
      ['Score basis', data.score_basis === 'candidate_creation' ? 'Recorded at candidate creation' : 'Current qualification'],
      ['Bullish score', data.bullish_score], ['Bearish score', data.bearish_score],
      ['Entry trigger', trigger ? `${trigger.type} · ${format(trigger.level)} · ${trigger.confirmation} · ${trigger.instrument}` : null],
      ['Trigger status', triggerStatus],
      ['Last recorded breach', watch.breached_at ? `BREACHED · ${stamp(watch.breached_at)} @ ${format(watch.breach_price)}` : null],
      ['1m structure', early ? earlyStructure.direction || 'DEVELOPING' : earlyStructure.direction],
      ['Retest / rejection', early ? (earlyStructure.retest_rejection ? 'CONFIRMED' : earlyStructure.continuation_structure ? 'CONTINUATION STRUCTURE' : 'PENDING') : null],
      ['5m confirmation', early ? 'PENDING' : record?.state === 'CONFIRMED' ? 'CONFIRMED' : null],
      ['Risk status', early ? 'EARLY / MANUAL' : null],
      ['Invalidation', plan?.invalidation?.level], ['T1', plan?.target1?.level], ['T2', plan?.target2?.level],
      ['T1 R:R (plan)', plan?.t1_rr], ['T2 R:R (plan)', plan?.t2_rr],
      ['Current underlying', entry.underlying], ['T1 R:R (current price)', entry.t1_rr],
      ['Minimum entry R:R', entry.minimum_t1_rr],
      ['Candidate deadline', active && ['CANDIDATE','EARLY_SETUP'].includes(data.state) ? stamp(entry.expires_at || record?.expires_at) : null],
      ['T1 R:R (confirmation)', record?.confirmed_t1_rr],
      ['Selected option', option ? `${option.trading_symbol} · ${option.expiry} · ${option.strike} ${option.option_type}` : null],
      ['Liquidity at selection', option ? `Qualified · spread ${format(option.spread_percent)}% · OI ${format(option.oi)} · volume ${format(option.volume)}` : null],
      ['Last updated', stamp(data.last_updated)]
    ];
    if (outcome?.entry_time) rows.push(['Entry observed', stamp(outcome.entry_time)],
      ['Entry underlying', outcome.entry_underlying], ['Entry option LTP', outcome.entry_option_ltp],
      ['MFE / MAE (underlying)', `${format(outcome.mfe)} / ${format(outcome.mae)}`],
      ['Result (model R)', outcome.result_r], ['Duration (seconds)', outcome.duration_seconds],
      ['Observation gaps', outcome.observation_gap ? 'Yes — excursions may be incomplete' : 'None detected']);
    const list = document.createElement('dl');
    for (const [label, value] of rows) {
      const term = document.createElement('dt'), detail = document.createElement('dd');
      term.textContent = label; detail.textContent = format(value); list.append(term, detail);
    }
    box.append(list);
    if (early) {
      const warning = document.createElement('p'); warning.className = 'signal-early-warning';
      warning.textContent = 'This is an early setup before normal 5-minute confirmation. No automatic trade is placed.';
      box.append(warning);
    }
    section(box, 'Category breakdown', Object.entries(data.category_scores || {}).map(([name, score]) =>
      `${name.replaceAll('_', ' ')}: ${score.direction} · bullish ${format(score.bullish_points)} / bearish ${format(score.bearish_points)} · available ${format(score.available_weight)}`));
    section(box, 'Evidence', data.evidence || []);
    section(box, 'Contradictions', data.contradictions || []);
    const quality = data.data_quality || {};
    const options = (data.current_qualification || data).data_quality?.options_quality || {};
    const coverage = options.coverage || {}, near = options.near_atm_quality || {};
    const optionRows = [`Selected expiry: ${format(options.selected_expiry)}`,
      `Overall chain: ${format(coverage.fresh_contracts ?? coverage.received_contracts)} / ${format(coverage.expected_contracts)} (${format(coverage.percent)}%) · diagnostic only`,
      `Full chain fresh: ${options.full_chain_fresh == null ? 'Unavailable' : options.full_chain_fresh ? 'Yes' : 'No'} (informational)`,
      `Near ATM: ${format(near.fresh_contracts)} / ${format(near.expected_contracts)} (${format(near.percent)}%)`];
    for (const [side, row] of Object.entries(options.atm_quality || {})) optionRows.push(
      `ATM ${side.toUpperCase()}: ${row.fresh ? 'Fresh' : 'Stale / unavailable'} · ${row.liquidity} · ${row.reason}`);
    const labels = {positioning_flow:'Positioning flow', atm_behavior:'ATM behavior', oi_wall_breakout:'OI walls', pcr_confirmation:'PCR'};
    for (const [key, item] of Object.entries(options.component_availability || {})) optionRows.push(
      `${labels[key] || key}: ${format(item.available_weight)} / ${format(item.maximum_weight)} ${item.available_weight > 0 ? 'available' : 'unavailable'} · ${item.reason}`);
    section(box, 'OPTIONS DATA QUALITY', optionRows);
    const qualityRows = [quality.stale ? 'Stale / waiting for fresh data' : 'Current observation'];
    for (const [key, value] of Object.entries(quality)) {
      if (key === 'config' || key === 'stale' || key === 'options_quality') continue;
      qualityRows.push(`${key.replaceAll('_', ' ')}: ${Array.isArray(value) ? value.join('; ') || 'None' : format(value)}`);
    }
    section(box, 'Data quality', qualityRows);
  }
  let busy = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/signals/current', {signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error();
      const data = await response.json();
      for (const index of ['nifty', 'banknifty']) {
        const signal = data.signals.find(s => s.index === index.toUpperCase());
        if (!signal) throw new Error();
        paint(index, signal);
      }
    } catch {
      for (const index of ['nifty', 'banknifty']) paint(index, {decision:'NO_TRADE', confidence:0,
        evidence:[], contradictions:[], data_quality:{stale:true, blocking_reasons:['Signal observations unavailable; retrying.']}});
    } finally { busy = false; }
  }
  refresh(); setInterval(() => { if (window.marketAutoRefreshAllowed()) refresh(); }, 5000);
})();
