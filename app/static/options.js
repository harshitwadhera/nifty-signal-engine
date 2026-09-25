(() => {
  const get = id => document.getElementById(id);
  const format = value => value == null ? 'Unavailable' : typeof value === 'number'
    ? value.toLocaleString('en-IN', {maximumFractionDigits: 2}) : String(value);
  const stamp = value => value ? new Date(value).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) + ' IST' : 'Unavailable';
  const zones = rows => rows?.length ? rows.map(r => format(r.strike) + ' (' + format(r.value) + ')').join(', ') : 'Unavailable';
  function paint(index, data) {
    const container = get(index + '-options');
    container.replaceChildren();
    const badge = document.createElement('p');
    badge.className = data.stale ? 'stale' : 'live';
    badge.textContent = data.stale ? 'STALE / INCOMPLETE / MARKET CLOSED' : 'FRESH SNAPSHOT';
    container.append(badge);
    const rows = [
      ['Selected expiry', data.selected_expiry], ['Spot', data.spot], ['Expiry-matched future', data.futures],
      ['Front-month future', data.front_month_futures], ['ATM', data.atm],
      ['Full-expiry OI PCR', data.pcr_oi], ['Full-expiry volume PCR', data.pcr_volume], ['Max pain', data.max_pain],
      ['Call OI wall', data.call_oi_wall], ['Put OI wall', data.put_oi_wall],
      ['Top call OI additions (session)', zones(data.call_oi_additions)], ['Top put OI additions (session)', zones(data.put_oi_additions)]
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
      ' · Coverage: ' + (data.coverage?.received_contracts ?? 0) + '/' + (data.coverage?.expected_contracts ?? 0) +
      ' (' + format(data.coverage?.percent) + '%)';
    container.append(freshness);
    const selector = get(index + '-options-expiry');
    const selected = selector.value;
    selector.replaceChildren();
    for (const value of ['', ...(data.expiries || [])]) {
      const option = document.createElement('option'); option.value = value; option.textContent = value || 'Nearest listed';
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
      const response = await fetch('/api/options/' + index + (expiry ? '?expiry=' + encodeURIComponent(expiry) : ''), {signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error();
      paint(index, await response.json());
    } catch {
      const container = get(index + '-options'); container.replaceChildren();
      container.textContent = 'Options data unavailable. Retrying shortly.';
    } finally { busy[index] = false; }
  }
  for (const index of ['nifty', 'banknifty']) {
    get(index + '-options-expiry').addEventListener('change', () => refresh(index));
    refresh(index); setInterval(() => refresh(index), 5000);
  }
})();
