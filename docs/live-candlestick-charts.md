# Market Charts (Task 1)

Two browser-rendered SVG candlestick charts use the existing `/api/candles/NIFTY`
and `/api/candles/BANKNIFTY` endpoints. Initial, manual and timeframe-change loads
request `limit=100`; routine five-second refreshes request only `limit=3`.
Default: 5m. The existing aliases resolve to `NIFTY 50` and `NIFTY BANK`.
Cards sit side by side above 720px and stack at or below that width.

## Display and data

- OHLC, completion, partial coverage and source come directly from the backend.
  There is no browser aggregation, signal calculation, or timer-based closing of bars.
  The backend's existing five-second lateness window can temporarily return two
  forming bars near an interval boundary; both remain visible until completed.
- Forming bars have hollow bodies. Amber outlines mark partial bars. Blue dots
  identify non-live sources (historical recovery or mixed live/history). Counts and
  OHLC inspection retain completion, partial flags and the exact source string.
  Volume is neither required nor displayed.
- Completed OHLC stays unchanged unless the backend explicitly supplies a
  correction, such as historical recovery repairing a partial candle. The browser
  accepts those authoritative corrections rather than freezing bad history.
- Time labels explicitly use `Intl.DateTimeFormat` with `Asia/Kolkata`. Parsing
  requires timestamps with a UTC marker or numeric offset; naive timestamps are
  rejected. Session gaps are compressed, with dates on the time axis and readout.
- Hover/tap inspects a bar; focused charts support arrow keys, Home and End.
  The latest close and the actual displayed interval remain visible above each chart.
  If a timeframe switch fails, the old chart keeps its old interval label.
- Empty history, disconnected feed, HTTP/network/timeout errors, and outdated
  candles have explicit messages. Failed/empty refreshes preserve a usable chart.
  Feed status reuses the existing dashboard indicators through one DOM observer;
  it adds no connection or quote requests. A successful candle fetch is not a
  guarantee of tick freshness. The existing API has no per-candle last-tick age;
  a forming candle may be stale while the market-wide feed indicator remains fresh.

## Polling and resources

One new shared five-second timer calls the existing `marketAutoRefreshAllowed()`.
It fetches both charts only Monday–Friday, from 09:00 inclusive to 15:40 exclusive
IST. Initial loads and explicit timeframe/Refresh chart actions work at any time.
NSE holidays are not detected, matching the existing helper. Requests started
before the cutoff may finish afterward; no new automatic requests start outside
the window. This does not change active-trade checks or alarm timers.

Each chart permits only one request at a time. A timeframe change aborts the old
request, waits for it to settle, and loads only the latest selection. Generation
checks reject late responses. Polls and manual refreshes skip an in-flight request.
Requests have a ten-second timeout. Automatic refreshes merge the latest three
authoritative backend candles by start time into the browser's existing 100-candle
buffer. Matching candles are replaced so forming bars, completion transitions and
historical recovery corrections remain authoritative. If the incremental response
is malformed, has no overlap with the current buffer, moves backward, or introduces
an unexpected same-session gap, the browser falls back to a full 100-candle reload.
The SVG root, axes, and surviving candle nodes are reused; at most 100 candles are
retained. Listeners, resize observers, and timers are installed once, not per refresh.

Incremental load per open dashboard tab during the permitted window remains two
HTTP reads every five seconds (0.4 requests/second, 1,440/hour, about 9,600 over
the 400-minute window), plus explicit manual actions, but routine responses now
contain at most three bars instead of 100. Using the previous 25–40 KB estimate for
100 bars as a rough scale, routine responses should normally be around 1–2 KB plus
HTTP overhead, or roughly 10–20 MB of uncompressed candle JSON per full trading
window per tab before occasional full-reload fallbacks. Actual size depends on
fields, headers and compression. This is an estimate, not an EC2 measurement.
Rendering runs in the browser. Server work is existing memory/SQLite reads and
JSON serialization; there are no extra Zerodha REST/history calls, writes, or
background workers from chart requests. Multiple open tabs multiply the load.

## Scope and verification

No library, CDN, build step, framework, external runtime dependency, or license
bundle was added. SVG is created with the native `createElementNS` API. CSP is
unchanged. Current browsers need `AbortSignal.any`/`timeout`, as well as SVG and
Intl; resize observation is optional. No zoom/pan, EMA/VWAP overlays, order markers,
or AI features are included in this first version.

Backend production files, SQLite schema, authentication, WebSocket processing,
options freshness/calculations, signal scoring, and trade/stop/target behavior
are unchanged. Tests cover both symbols and all three UI intervals, limits,
invalid intervals, current/completed bars and the absence of broker calls.
Browser logic tests cover initial full loads, three-candle incremental refreshes,
merge/correction behavior, full-reload fallback on gaps or malformed recent data,
the IST polling boundaries/weekends, manual refresh, timeframe races, no overlapping
calls, preserved charts on error, partial/recovery annotations, bounded nodes,
resize and timezone independence.

Run `node --test tests/*.test.cjs` and `.\.venv\Scripts\python.exe -m pytest -q`.
DOM doubles test behavior, not pixel layout; check desktop and mobile in a real
browser before deployment. No live Zerodha session or EC2 benchmark is required
or claimed by these tests.

Native browser API references: [SVG creation](https://developer.mozilla.org/en-US/docs/Web/API/Document/createElementNS)
and [combined cancellation signals](https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal/any_static).
