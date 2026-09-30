(() => {
  const get = id => document.getElementById(id);
  const format = value => value == null ? '—' : typeof value === 'number'
    ? value.toLocaleString('en-IN', {maximumFractionDigits: 2}) : String(value);
  const when = value => value ? new Date(value).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short',
    hour: '2-digit', minute: '2-digit', hour12: true
  }) : '—';

  function states(item) {
    return new Set((Array.isArray(item.history) ? item.history : []).map(event => event?.state));
  }

  function modelResult(item) {
    const seen = states(item);
    if (seen.has('TARGET2_HIT')) return 'T2 HIT';
    if (seen.has('STOPPED')) return seen.has('TARGET1_HIT') ? 'T1 HIT → STOPPED' : 'STOPPED';
    if (seen.has('EXPIRED')) return seen.has('TARGET1_HIT') ? 'T1 HIT → EXPIRED' : 'EXPIRED';
    if (seen.has('TARGET1_HIT')) return 'T1 HIT';
    return item.state === 'CONFIRMED' ? 'CONFIRMED / ACTIVE' : format(item.state);
  }

  function cell(value, className) {
    const node = document.createElement('td');
    node.textContent = format(value);
    if (className) node.className = className;
    return node;
  }

  function render(data) {
    const box = get('opportunity-history');
    box.replaceChildren();
    const items = data.items || [];
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'opportunity-empty';
      empty.textContent = 'No confirmed trade opportunities recorded yet.';
      box.append(empty);
      return;
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'opportunity-table';
    const table = document.createElement('table');
    const head = document.createElement('thead');
    const header = document.createElement('tr');
    for (const title of ['Confirmed', 'Index', 'Signal', 'Option', 'Confirmation / model entry', 'Stop', 'T1', 'T2', 'Model result', 'Taken?']) {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = title;
      header.append(th);
    }
    head.append(header);
    table.append(head);

    const body = document.createElement('tbody');
    for (const item of items) {
      const plan = item.plan || {};
      const option = plan.option || {};
      const entry = item.confirmation_price ?? item.outcome?.entry_underlying;
      const row = document.createElement('tr');
      row.append(
        cell(when(item.confirmed_at)),
        cell(item.index_name),
        cell(item.direction),
        cell(option.trading_symbol),
        cell(entry),
        cell(plan.invalidation?.level),
        cell(plan.target1?.level),
        cell(plan.target2?.level),
        cell(modelResult(item)),
        cell(item.taken ? 'YES' : 'NO', item.taken ? 'opportunity-taken-yes' : 'opportunity-taken-no')
      );
      body.append(row);
    }
    table.append(body);
    wrapper.append(table);
    box.append(wrapper);
  }

  let busy = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/signals/opportunities?limit=25', {signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error();
      const data = await response.json();
      render(data);
      get('opportunity-history-status').textContent =
        `Showing ${data.items?.length || 0} of ${data.total || 0} confirmed opportunities`;
    } catch {
      const box = get('opportunity-history');
      box.replaceChildren();
      const error = document.createElement('p');
      error.className = 'opportunity-empty';
      error.textContent = 'Confirmed opportunity history is temporarily unavailable.';
      box.append(error);
      get('opportunity-history-status').textContent = 'History unavailable. Retrying during market hours.';
    } finally {
      busy = false;
    }
  }

  refresh();
  setInterval(() => {
    if (window.marketAutoRefreshAllowed()) refresh();
  }, 10000);
})();
