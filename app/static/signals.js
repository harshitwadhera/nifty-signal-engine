(() => {
  const get = id => document.getElementById(id);
  const format = v => v == null ? 'Unavailable' : typeof v === 'number'
    ? v.toLocaleString('en-IN', {maximumFractionDigits: 2}) : String(v);
  const stamp = v => v ? new Date(v).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST' : 'Waiting';
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
  refresh(); setInterval(refresh, 5000);
})();
