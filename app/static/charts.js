/* Browser-only SVG renderer. Candle OHLC/completion/provenance come from the backend. */
(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const intervals = ['5m', '15m', '30m'];
  const FULL_LIMIT = 100, INCREMENTAL_LIMIT = 3;
  const ZOOM_LEVELS = [15, 30, 60, 100];
  const clock = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
  const day = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short'});
  const calendarDay = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit'});
  const price = value => value.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const stamp = value => `${day.format(value)} ${clock.format(value)} IST`;
  const attrs = (node, values) => { for (const [key, value] of Object.entries(values)) node.setAttribute(key, value); };
  function svg(tag, values = {}, text = '') {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    attrs(node, values); node.textContent = text; return node;
  }
  class CandleDataError extends Error {}
  function awareTime(value) {
    // Refuse naive timestamps instead of interpreting them in the device timezone.
    if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/i.test(value)) return NaN;
    return Date.parse(value);
  }
  function candlesFrom(data, symbol, interval) {
    if (data?.symbol !== symbol || data.interval !== interval || !Array.isArray(data.candles) || data.candles.length > 100) {
      throw new CandleDataError('Unexpected candle response.');
    }
    let previous = -Infinity;
    return data.candles.map(c => {
      const start = awareTime(c.start_time), end = awareTime(c.end_time);
      if (!Number.isFinite(start) || !Number.isFinite(end) || start <= previous || end <= start ||
          !['open', 'high', 'low', 'close'].every(key => typeof c[key] === 'number' && Number.isFinite(c[key])) ||
          c.low > Math.min(c.open, c.close) || c.high < Math.max(c.open, c.close) ||
          typeof c.completed !== 'boolean' || typeof c.partial !== 'boolean' || typeof c.source !== 'string') {
        throw new CandleDataError('Invalid candle data.');
      }
      previous = start;
      return {...c, start, end};
    });
  }
  function mergeIncremental(current, incoming, interval) {
    if (!current.length || !incoming.length) return null;
    const known = new Map(current.map(c => [c.start, c]));
    if (!incoming.some(c => known.has(c.start)) || incoming.at(-1).start < current.at(-1).start) return null;
    const duration = Number.parseInt(interval, 10) * 60000;
    const sameSessionDay = (a, b) => calendarDay.format(a) === calendarDay.format(b);
    for (let i = 1; i < incoming.length; i++) {
      const previous = incoming[i - 1], next = incoming[i];
      if (sameSessionDay(previous.start, next.start) && next.start - previous.start !== duration) return null;
    }
    const firstNew = incoming.find(c => c.start > current.at(-1).start);
    if (firstNew) {
      const previous = current.at(-1);
      if (sameSessionDay(previous.start, firstNew.start) && firstNew.start - previous.start !== duration) return null;
    }
    for (const candle of incoming) known.set(candle.start, candle);
    return [...known.values()].sort((a, b) => a.start - b.start).slice(-FULL_LIMIT);
  }
  function description(c) {
    return `${stamp(c.start)} – ${clock.format(c.end)} IST · O ${price(c.open)}  H ${price(c.high)}  L ${price(c.low)}  C ${price(c.close)}\n` +
      `${c.completed ? 'Closed' : 'Forming'} · Source: ${c.source}${c.partial ? ' · PARTIAL coverage' : ''}`;
  }

  class CandleChart {
    constructor(id, symbol, resolved) {
      this.id = id; this.symbol = symbol; this.resolved = resolved;
      this.interval = '5m'; this.loadedInterval = null; this.rows = []; this.nodes = new Map();
      this.busy = false; this.version = 0; this.error = ''; this.selected = null;
      this.visibleCount = FULL_LIMIT; this.anchorStart = null; this.visibleRows = []; this.dragX = null; this.step = 1;
      this.frame = get(id + '-chart-frame');
      this.root = svg('svg', {class: 'candle-chart', role: 'img', tabindex: '0',
        'aria-label': `${symbol} candlestick chart. Arrow keys inspect candles.`});
      this.axes = svg('g', {class: 'chart-axes'});
      this.bars = svg('g');
      this.cursor = svg('line', {class: 'chart-cursor', visibility: 'hidden'});
      this.latest = svg('line', {class: 'chart-latest', visibility: 'hidden'});
      this.latestLabel = svg('text', {class: 'chart-latest-label'});
      this.empty = svg('text', {class: 'chart-empty', x: '50%', y: '50%', 'text-anchor': 'middle'}, 'Loading candles…');
      this.root.append(this.axes, this.bars, this.cursor, this.latest, this.latestLabel, this.empty);
      this.frame.append(this.root);
      this.priceTicks = Array.from({length: 5}, () => {
        const line = svg('line'), label = svg('text'); this.axes.append(line, label); return {line, label};
      });
      this.timeTicks = Array.from({length: 3}, () => {
        const time = svg('text'), date = svg('text'); this.axes.append(time, date); return {time, date};
      });
      const inspect = event => {
        const bar = event.target.closest('[data-start]');
        if (bar) { this.selected = Number(bar.getAttribute('data-start')); this.readout(); }
      };
      this.root.addEventListener('pointerdown', event => {
        if (event.button != null && event.button !== 0) return;
        this.dragX = event.clientX;
      });
      this.root.addEventListener('pointermove', event => {
        if (this.dragX != null && Number.isFinite(event.clientX)) {
          const delta = event.clientX - this.dragX;
          const steps = Math.trunc(delta / Math.max(this.step, 1));
          if (steps) {
            event.preventDefault?.();
            this.pan(-steps);
            this.dragX += steps * Math.max(this.step, 1);
          }
          return;
        }
        inspect(event);
      });
      const endDrag = () => { this.dragX = null; };
      this.root.addEventListener('pointerup', endDrag);
      this.root.addEventListener('pointercancel', endDrag);
      this.root.addEventListener('click', inspect);
      this.root.addEventListener('pointerleave', () => {
        endDrag(); this.selected = null; this.readout();
      });
      this.root.addEventListener('wheel', event => {
        if (!this.rows.length || !Number.isFinite(event.deltaY) || event.deltaY === 0) return;
        event.preventDefault?.();
        this.zoom(event.deltaY < 0 ? -1 : 1);
      }, {passive: false});
      this.root.addEventListener('keydown', event => {
        const visible = this.visibleRows.length ? this.visibleRows : this.rows;
        if (!visible.length || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        let index = visible.findIndex(c => c.start === this.selected);
        if (index < 0) index = visible.length - 1;
        index = event.key === 'Home' ? 0 : event.key === 'End' ? visible.length - 1 : index + (event.key === 'ArrowLeft' ? -1 : 1);
        this.selected = visible[Math.max(0, Math.min(visible.length - 1, index))].start;
        this.readout();
      });
      get(id + '-chart-interval').addEventListener('change', event => {
        const next = event.target.value;
        if (!intervals.includes(next) || next === this.interval) return;
        this.interval = next; this.version++; this.error = ''; this.resetView(false);
        // Serialize requests even if several selections change before an abort settles.
        if (this.busy) this.controller.abort();
        else this.refresh(true);
        this.status();
      });
      get(id + '-chart-refresh').addEventListener('click', () => this.refresh(true));
      get(id + '-chart-zoom-in').addEventListener('click', () => this.zoom(-1));
      get(id + '-chart-zoom-out').addEventListener('click', () => this.zoom(1));
      get(id + '-chart-reset').addEventListener('click', () => this.resetView());
      if (typeof ResizeObserver !== 'undefined') {
        this.resize = new ResizeObserver(() => this.render()); this.resize.observe(this.frame);
      }
      this.render(); this.refresh(true);
    }
    visibleWindow() {
      if (!this.rows.length) return {rows: [], start: 0, end: 0, pinned: true};
      const count = Math.min(this.visibleCount, this.rows.length);
      if (count >= this.rows.length) {
        this.anchorStart = null;
        return {rows: this.rows, start: 0, end: this.rows.length, pinned: true};
      }
      let end = this.rows.length;
      if (this.anchorStart != null) {
        const anchor = this.rows.findIndex(c => c.start === this.anchorStart);
        if (anchor >= 0) end = anchor + 1;
        else {
          end = count;
          this.anchorStart = this.rows[end - 1].start;
        }
      }
      end = Math.max(count, Math.min(this.rows.length, end));
      const start = Math.max(0, end - count);
      return {rows: this.rows.slice(start, end), start, end, pinned: end === this.rows.length && this.anchorStart == null};
    }
    updateViewControls() {
      const level = ZOOM_LEVELS.indexOf(this.visibleCount);
      get(this.id + '-chart-zoom-in').disabled = !this.rows.length || level <= 0;
      get(this.id + '-chart-zoom-out').disabled = !this.rows.length || level === ZOOM_LEVELS.length - 1;
      get(this.id + '-chart-reset').disabled = !this.rows.length || (this.visibleCount === FULL_LIMIT && this.anchorStart == null);
    }
    zoom(direction) {
      if (!this.rows.length) return;
      const index = ZOOM_LEVELS.indexOf(this.visibleCount);
      const next = Math.max(0, Math.min(ZOOM_LEVELS.length - 1, index + direction));
      if (next === index) return;
      this.visibleCount = ZOOM_LEVELS[next];
      if (this.visibleCount >= this.rows.length) this.anchorStart = null;
      this.selected = null;
      this.render();
    }
    pan(delta) {
      if (!this.rows.length || !Number.isFinite(delta) || delta === 0) return;
      const view = this.visibleWindow();
      if (view.rows.length >= this.rows.length) return;
      const count = view.rows.length;
      const end = Math.max(count, Math.min(this.rows.length, view.end + delta));
      this.anchorStart = end === this.rows.length ? null : this.rows[end - 1].start;
      this.selected = null;
      this.render();
    }
    resetView(render = true) {
      this.visibleCount = FULL_LIMIT;
      this.anchorStart = null;
      this.selected = null;
      if (render) this.render();
    }
    status() {
      const parts = [];
      if (this.busy && (!this.rows.length || this.loadedInterval !== this.interval)) parts.push(`Loading ${this.interval}…`);
      if (this.error) parts.push(this.error + (this.rows.length ? ' Showing saved chart; it may be stale.' : ' Use Refresh chart to retry.'));
      if (this.rows.length) parts.push(`Showing ${this.loadedInterval} · ${this.rows.length} candles · fetched ${stamp(this.fetchedAt)}.`);
      else if (!this.busy && !this.error) parts.push('No candle history yet. Connect to Zerodha and wait for ticks or history recovery.');
      if (!window.marketAutoRefreshAllowed()) parts.push('Automatic refresh paused outside market hours.');
      // Reuse the dashboard's existing connection/freshness indicators; no extra auth or quote requests.
      const connection = get('status')?.textContent;
      if (connection === 'DISCONNECTED') parts.push('Zerodha disconnected. Connect to resume the feed.');
      else if (connection !== 'CONNECTED') parts.push('Feed status unavailable.');
      else if (get('freshness')?.className !== 'live') parts.push('Feed stale / market closed.');
      if (this.rows.length && this.rows.at(-1).end < Date.now() - 15000) parts.push('No recent candle.');
      get(this.id + '-chart-status').textContent = parts.join(' ');
      get(this.id + '-chart-refresh').disabled = this.busy;
      this.updateViewControls();
      this.frame.setAttribute('aria-busy', this.busy);
    }
    async request(interval, limit) {
      const response = await fetch(`/api/candles/${this.symbol}?interval=${interval}&limit=${limit}`, {
        signal: AbortSignal.any([this.controller.signal, AbortSignal.timeout(10000)])
      });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? 'Connect to Zerodha to load candles.' : 'Candle service unavailable.');
      return candlesFrom(await response.json(), this.resolved, interval);
    }
    async refresh(forceFull = false) {
      if (this.busy) return;
      const version = this.version, interval = this.interval;
      this.busy = true; this.controller = new AbortController(); this.status();
      if (!this.rows.length) this.empty.textContent = 'Loading candles…';
      try {
        const full = forceFull || !this.rows.length || this.loadedInterval !== interval;
        let rows;
        if (full) {
          rows = await this.request(interval, FULL_LIMIT);
        } else {
          let recent = null;
          try {
            recent = await this.request(interval, INCREMENTAL_LIMIT);
          } catch (error) {
            if (!(error instanceof CandleDataError)) throw error;
          }
          if (version !== this.version) return;
          rows = recent && mergeIncremental(this.rows, recent, interval);
          if (!rows) rows = await this.request(interval, FULL_LIMIT);
        }
        if (version !== this.version) return;
        if (!rows.length && this.rows.length) throw new Error(`No ${interval} candles returned.`);
        if (this.loadedInterval !== interval) this.selected = null;
        this.rows = rows; this.loadedInterval = interval; this.fetchedAt = Date.now(); this.error = '';
        this.render();
      } catch (error) {
        if (version === this.version) {
          this.error = error.name === 'TimeoutError' ? 'Candle request timed out.' :
            error instanceof TypeError ? 'Candle service unavailable.' : error.message;
          if (!this.rows.length) this.empty.textContent = 'Candles unavailable';
        }
      } finally {
        this.busy = false;
        if (!this.rows.length) this.render();
        this.status();
        // Only an explicit timeframe change queues another full request, even after hours.
        if (version !== this.version) this.refresh(true);
      }
    }
    render() {
      const width = Math.max(260, this.frame.clientWidth || 420), right = width - 76, top = 18, bottom = 228;
      this.root.setAttribute('viewBox', `0 0 ${width} 282`);
      this.empty.textContent = this.rows.length ? '' : this.error ? 'Candles unavailable' : this.busy ? 'Loading candles…' : 'No candle history yet';
      this.axes.setAttribute('visibility', this.rows.length ? 'visible' : 'hidden');
      if (!this.rows.length) {
        this.visibleRows = [];
        this.updateViewControls();
        get(this.id + '-chart-readout').textContent = 'No candles to inspect.';
        return;
      }
      const view = this.visibleWindow(), visible = view.rows;
      this.visibleRows = visible;
      const low = Math.min(...visible.map(c => c.low)), high = Math.max(...visible.map(c => c.high));
      const padding = Math.max((high - low) * 0.08, Math.abs(high) * 0.0001, 0.05);
      const min = low - padding, max = high + padding;
      const y = value => bottom - (value - min) / (max - min) * (bottom - top);
      const step = (right - 8) / visible.length, bodyWidth = Math.max(1, Math.min(14, step * 0.7));
      this.step = step;
      this.x = index => 8 + step * (index + 0.5);
      this.priceTicks.forEach(({line, label}, i) => {
        const value = max - (max - min) * i / 4, position = y(value);
        attrs(line, {x1: 8, x2: right, y1: position, y2: position});
        attrs(label, {x: right + 6, y: position + 4}); label.textContent = price(value);
      });
      const tickIndices = [...new Set([0, Math.round((visible.length - 1) / 2), visible.length - 1])];
      this.timeTicks.forEach(({time, date}, i) => {
        const index = tickIndices[i];
        attrs(time, {visibility: index === undefined ? 'hidden' : 'visible'});
        attrs(date, {visibility: index === undefined ? 'hidden' : 'visible'});
        if (index === undefined) return;
        const c = visible[index], anchor = i === 0 ? 'start' : i === tickIndices.length - 1 ? 'end' : 'middle';
        attrs(time, {x: this.x(index), y: 250, 'text-anchor': anchor}); time.textContent = clock.format(c.start);
        attrs(date, {x: this.x(index), y: 268, 'text-anchor': anchor}); date.textContent = day.format(c.start);
      });
      const present = new Set(visible.map(c => c.start));
      for (const [key, node] of this.nodes) if (!present.has(key)) { node.group.remove(); this.nodes.delete(key); }
      visible.forEach((c, i) => {
        let node = this.nodes.get(c.start);
        if (!node) {
          node = {group: svg('g', {'data-start': c.start}), title: svg('title'), hit: svg('rect', {class: 'chart-hit'}),
            wick: svg('line', {class: 'chart-wick'}), body: svg('rect', {class: 'chart-body'}), origin: svg('circle', {class: 'chart-origin', r: 1.8})};
          node.group.append(node.title, node.hit, node.wick, node.body, node.origin);
          this.bars.append(node.group); this.nodes.set(c.start, node);
        }
        node.group.setAttribute('class', `chart-candle ${c.close >= c.open ? 'chart-up' : 'chart-down'}${c.partial ? ' chart-partial' : ''}${!c.completed ? ' chart-forming' : ''}`);
        node.title.textContent = description(c);
        const x = this.x(i);
        attrs(node.hit, {x: x - step / 2, y: top, width: step, height: bottom - top});
        attrs(node.wick, {x1: x, x2: x, y1: y(c.high), y2: y(c.low)});
        attrs(node.body, {x: x - bodyWidth / 2, y: y(Math.max(c.open, c.close)), width: bodyWidth, height: Math.max(1, Math.abs(y(c.open) - y(c.close)))});
        attrs(node.origin, {cx: x, cy: bottom + 6, visibility: c.source === 'live' ? 'hidden' : 'visible'});
      });
      const last = this.rows.at(-1);
      if (view.end === this.rows.length) {
        const position = y(last.close);
        attrs(this.latest, {x1: 8, x2: right, y1: position, y2: position, visibility: 'visible'});
        this.priceTicks.forEach(({label}) => label.setAttribute('visibility', Math.abs(Number(label.getAttribute('y')) - position - 4) < 15 ? 'hidden' : 'visible'));
        attrs(this.latestLabel, {x: right + 6, y: position + 4, visibility: 'visible'});
        this.latestLabel.textContent = price(last.close);
      } else {
        this.latest.setAttribute('visibility', 'hidden');
        this.latestLabel.setAttribute('visibility', 'hidden');
      }
      get(this.id + '-chart-price').textContent = `${price(last.close)} · ${this.loadedInterval}`;
      get(this.id + '-chart-quality').textContent = `${this.rows.filter(c => c.partial).length} partial · ${this.rows.filter(c => c.source !== 'live').length} recovered / mixed · ${this.rows.filter(c => !c.completed).length} forming · View: ${visible.length}/${this.rows.length} ${view.end === this.rows.length ? 'latest' : 'history'} · Time: IST`;
      this.root.setAttribute('aria-label', `${this.symbol} ${this.loadedInterval} candlestick chart, latest close ${price(last.close)}, showing ${visible.length} of ${this.rows.length} candles. Wheel or zoom buttons change scale; drag horizontally to pan; arrow keys inspect visible candles.`);
      this.updateViewControls();
      this.readout();
    }
    readout() {
      const visible = this.visibleRows.length ? this.visibleRows : this.rows;
      if (!visible.length) return;
      let index = visible.findIndex(c => c.start === this.selected);
      if (index < 0) index = visible.length - 1;
      get(this.id + '-chart-readout').textContent = description(visible[index]);
      attrs(this.cursor, {x1: this.x(index), x2: this.x(index), y1: 18, y2: 228, visibility: this.selected === null ? 'hidden' : 'visible'});
    }
  }
  const charts = [new CandleChart('nifty', 'NIFTY', 'NIFTY 50'), new CandleChart('banknifty', 'BANKNIFTY', 'NIFTY BANK')];
  // A single shared timer. Per-chart in-flight guards also cover manual refreshes.
  setInterval(() => {
    for (const chart of charts) {
      chart.status();
      if (window.marketAutoRefreshAllowed()) chart.refresh(false);
    }
  }, 5000);
  if (typeof MutationObserver !== 'undefined') {
    const observer = new MutationObserver(() => charts.forEach(chart => chart.status()));
    for (const id of ['status', 'freshness']) observer.observe(get(id), {childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ['class']});
  }
})();
