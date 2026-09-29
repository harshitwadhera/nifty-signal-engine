(() => {
  const get = id => document.getElementById(id);
  const format = v => v == null ? 'Unavailable' : typeof v === 'number'
    ? v.toLocaleString('en-IN', {maximumFractionDigits: 2}) : String(v);
  const stamp = v => v ? new Date(v).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST' : 'Waiting';
  const expiryDate = v => v ? new Date(v + 'T00:00:00+05:30').toLocaleDateString('en-GB',
    {day: '2-digit', month: 'short', year: 'numeric', timeZone: 'Asia/Kolkata'}) : 'Unavailable';
  const categoryLabels = {price_trend: 'Price / Trend', options_positioning: 'Options',
    breadth_constituents: 'Breadth', volatility: 'VIX', futures_structure: 'Futures'};
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
    const gates = data.qualification_gates || [];
    table(parent, data.decision === 'NO_TRADE' ? 'WHY NO TRADE' : 'CURRENT QUALIFICATION',
      ['Status', 'Gate', 'Observation / Requirement'], gates.length ? gates.map(g =>
        [g.status, g.label, [g.actual == null ? null : format(g.actual),
          g.required == null ? null : `${format(g.required)} required`].filter(Boolean).join(' / ') +
          (g.detail ? ` · ${g.detail}` : '')]) : [['INFO', 'Qualification', 'Waiting for backend gate details']]);
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
    const box = get(index + '-signal'); box.replaceChildren();
    const badge = document.createElement('p');
    badge.className = data.decision === 'NO_TRADE' ? 'signal-neutral' : 'signal-direction';
    badge.textContent = (data.decision || 'NO_TRADE').replaceAll('_', ' ');
    box.append(badge);
    qualification(box, data);
    const record = data.record, plan = record?.plan, outcome = record?.outcome;
    const trigger = plan?.entry_trigger, option = plan?.option;
    const rows = [
      ['State', data.state || 'No active signal'], ['Confidence (score / 100)', data.confidence],
      ['Freshness', data.data_quality?.stale ? 'STALE / waiting for complete data' : 'Current observation'],
      ['Score basis', data.score_basis === 'candidate_creation' ? 'Recorded at candidate creation' : 'Current qualification'],
      ['Bullish score', data.bullish_score], ['Bearish score', data.bearish_score],
      ['Entry trigger', trigger ? `${trigger.type} · ${format(trigger.level)} · ${trigger.confirmation} · ${trigger.instrument}` : null],
      ['Invalidation', plan?.invalidation?.level], ['T1', plan?.target1?.level], ['T2', plan?.target2?.level],
      ['T1 R:R (plan)', plan?.t1_rr], ['T2 R:R (plan)', plan?.t2_rr],
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
    section(box, 'Category breakdown', Object.entries(data.category_scores || {}).map(([name, score]) =>
      `${name.replaceAll('_', ' ')}: ${score.direction} · bullish ${format(score.bullish_points)} / bearish ${format(score.bearish_points)} · available ${format(score.available_weight)}`));
    section(box, 'Evidence', data.evidence || []);
    section(box, 'Contradictions', data.contradictions || []);
    const quality = data.data_quality || {};
    const qualityRows = [quality.stale ? 'Stale / waiting for fresh data' : 'Current observation'];
    for (const [key, value] of Object.entries(quality)) {
      if (key === 'config' || key === 'stale') continue;
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
