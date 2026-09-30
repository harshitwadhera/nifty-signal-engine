(() => {
  const get = id => document.getElementById(id);
  const format = value => value == null ? 'Unavailable' : typeof value === 'number'
    ? value.toLocaleString('en-IN', {maximumFractionDigits: 2}) : String(value);
  const stamp = value => value ? new Date(value).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST' : 'Unavailable';
  const zones = (rows, field = 'value') => rows?.length ? rows.map(r => format(r.strike) + ' (' + format(r[field]) + ')').join(', ') : 'Unavailable';
  function paint(index, data) {
    const container = get(index + '-options');
    container.replaceChildren();
    const badge = document.createElement('p');
    const available = data.options_total_available_weight;
    badge.className = available > 0 ? 'live' : 'stale';
    badge.textContent = available == null ? 'Options signal data unavailable' :
      `Options signal data: ${format(available)} / 30 available`;
    container.append(badge);
    const selection = data.expiry_selection || {}, policy = selection.analysis_expiry_policy || {};
    const rows = [
      ['Overall chain (diagnostic)', `${format(data.coverage?.fresh_contracts ?? data.coverage?.received_contracts)} / ${format(data.coverage?.expected_contracts)} (${format(data.coverage?.percent)}%)`],
      ['Selected expiry', data.selected_expiry], ['Automatic analysis expiry', selection.analysis_expiry],
      ['Analysis policy', policy.label], ['Analysis reason', policy.reason],
      ['Nearest listed expiry', selection.nearest], ['Nearest monthly expiry', selection.monthly],
      ['Spot', data.spot], ['Expiry-matched future', data.futures],
      ['Front-month future', data.front_month_futures], ['ATM', data.atm],
      ['Usable-strike OI PCR', data.pcr_oi], ['Usable-strike volume PCR', data.pcr_volume], ['Max pain', data.max_pain],
      ['Call OI wall', data.call_oi_wall], ['Put OI wall', data.put_oi_wall],
      ['Top call OI additions (session)', zones(data.call_oi_additions)], ['Top put OI additions (session)', zones(data.put_oi_additions)],
      ['Call writing zones (session)', zones(data.call_writing_zones, 'oi_change_session')],
      ['Put writing zones (session)', zones(data.put_writing_zones, 'oi_change_session')],
      ['Call unwinding zones (session)', zones(data.call_unwinding_zones)],
      ['Put unwinding zones (session)', zones(data.put_unwinding_zones)]
    ];
    for (const [kind, option] of [['CE', data.atm_ce], ['PE', data.atm_pe]]) {
      rows.push(['ATM ' + kind + ' LTP', option?.ltp], ['ATM ' + kind + ' OI', option?.oi],
        ['ATM ' + kind + ' ΔOI session', option?.oi_change_session], ['ATM ' + kind + ' ΔOI vs previous close', option?.oi_change_vs_previous_close],
        ['ATM ' + kind + ' volume', option?.volume], ['ATM ' + kind + ' IV (local %)', option?.iv == null ? null : option.iv * 100],
        ['ATM ' + kind + ' delta (local)', option?.delta], ['ATM ' + kind + ' spread', option?.spread],
        ['ATM ' + kind + ' liquidity', option?.liquidity_state], ['ATM ' + kind + ' model', option?.greeks_model]);
    }
    const list = document.createElement('dl');
    for (const [label, value] of rows) {
      const term = document.createElement('dt'), detail = document.createElement('dd');
      term.textContent = label; detail.textContent = format(value); list.append(term, detail);
    }
    container.append(list);
    const freshness = document.createElement('p');
    freshness.className = 'note';
    freshness.textContent = 'REST: ' + stamp(data.last_full_chain_refresh_at) + ' · Stream: ' + stamp(data.last_stream_tick_at) +
      ' · Coverage: ' + (data.coverage?.fresh_contracts ?? data.coverage?.received_contracts ?? 0) + '/' + (data.coverage?.expected_contracts ?? 0) +
      ' (' + format(data.coverage?.percent) + '%) · diagnostic only';
    container.append(freshness);
    const details = document.createElement('details'), title = document.createElement('summary');
    title.textContent = 'OPTIONS DATA QUALITY'; details.append(title);
    const quality = document.createElement('ul');
    const near = data.near_atm_quality || {};
    const qualityRows = [`Full chain fresh: ${data.full_chain_fresh == null ? 'Unavailable' : data.full_chain_fresh ? 'Yes' : 'No'} (informational)`,
      `Near ATM: ${format(near.fresh_contracts)} / ${format(near.expected_contracts)} (${format(near.percent)}%)`,
      'PCR uses matched fresh CE/PE strikes for each ratio. Max pain is informational only.'];
    for (const [side, row] of Object.entries(data.atm_quality || {})) qualityRows.push(
      `ATM ${side.toUpperCase()}: ${row.fresh ? 'Fresh' : 'Stale / unavailable'} · ${row.liquidity} · ${row.reason}`);
    const labels = {positioning_flow:'Positioning flow', atm_behavior:'ATM behavior', oi_wall_breakout:'OI walls', pcr_confirmation:'PCR'};
    for (const [key, item] of Object.entries(data.component_availability || {})) qualityRows.push(
      `${labels[key] || key}: ${format(item.available_weight)} / ${format(item.maximum_weight)} ${item.available_weight > 0 ? 'available' : 'unavailable'} · ${item.reason}`);
    for (const text of qualityRows) {
      const item = document.createElement('li'); item.textContent = text; quality.append(item);
    }
    details.append(quality); container.append(details);
    const selector = get(index + '-options-expiry');
    const selected = selector.value;
    selector.replaceChildren();
    for (const value of ['', ...(data.expiries || [])]) {
      const option = document.createElement('option'); option.value = value; option.textContent = value || 'Automatic analysis expiry';
      selector.append(option);
    }
    selector.value = (data.expiries || []).includes(selected) ? selected : '';
  }
  const busy = {};
  async function refresh(index) {
    if (busy[index]) return;
    busy[index] = true;
    try {
      const expiry = get(index + '-options-expiry').value;
      let response = await fetch('/api/options/' + index + (expiry ? '?expiry=' + encodeURIComponent(expiry) : ''), {signal: AbortSignal.timeout(15000)});
      if (response.status === 404 && expiry && get(index + '-options-expiry').value === expiry) {
        get(index + '-options-expiry').value = '';
        response = await fetch('/api/options/' + index, {signal: AbortSignal.timeout(15000)});
      }
      if (!response.ok) throw new Error();
      paint(index, await response.json());
    } catch {
      const container = get(index + '-options'); container.replaceChildren();
      container.textContent = 'Options data unavailable. Retrying shortly.';
    } finally { busy[index] = false; }
  }
  for (const index of ['nifty', 'banknifty']) {
    get(index + '-options-expiry').addEventListener('change', () => refresh(index));
    refresh(index); setInterval(() => { if (window.marketAutoRefreshAllowed()) refresh(index); }, 5000);
  }
})();
