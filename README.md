# NIFTY market dashboard — Phase 4

Local, single-user, read-only FastAPI application using the official `kiteconnect` Python SDK and KiteTicker. Displays NIFTY 50 (`NSE:NIFTY 50`), BANK NIFTY (`NSE:NIFTY BANK`) and INDIA VIX (`NSE:INDIA VIX`). Adds front-month NIFTY/BANKNIFTY futures, candles, futures VWAP, spot market structure and completed-candle EMA9/EMA20. Adds descriptive NIFTY/BANKNIFTY options analytics in Phase 4. No signals, recommendations, orders, automatic trading, RSI or MACD. Phases 1–3 were live-tested successfully by the user with their real Zerodha account.

## Windows setup (PowerShell)

Install Python 3.11 or newer, including the Windows Python launcher, then open PowerShell in this project:

```powershell
cd "C:\Users\harsh\OneDrive\Documents\ChatGPT\nifty-signal-engine"
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

If script activation is restricted, activation is optional: use `.\.venv\Scripts\python.exe` wherever `python` appears below, including dependency installation.

Put your credentials **only in the project-root `.env` file**, alongside `requirements.txt`:

```dotenv
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
KITE_REDIRECT_URL=http://127.0.0.1:8000/kite/callback
```

The placeholders above are not credentials. Existing environment variables take precedence over `.env`. Restart after changing configuration. Do not commit `.env` or share its contents.

In your [Kite developer console](https://developers.kite.trade/), set the app's registered redirect URL to exactly `http://127.0.0.1:8000/kite/callback`. `KITE_REDIRECT_URL` describes that registered URL; setting it locally does not update the Kite console. Use a Kite app with market quote API access.

## Test and run

```powershell
python -m pytest -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open [the dashboard](http://127.0.0.1:8000/) and click **Connect to Zerodha**. Complete username/password/TOTP manually on Zerodha's own page. The callback exchanges the request token server-side and redirects to the dashboard. Keep the browser on `127.0.0.1` throughout the flow so the login cookie matches. Login attempts expire after 10 minutes and cannot be replayed.

Run exactly one worker and one application process. Do not enable auto-reload if you want to retain the in-memory session. Duplicate feed prevention is per application process; multiple workers are unsupported. This app is intended for loopback use only, not public hosting or multiple users. If this directory is synced by OneDrive, exclude `.env` from syncing or use a nonsynced local project directory for real credentials.

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET /` | Dashboard, polls live state every 2 seconds |
| `GET /health` | Process liveness; does not check Kite |
| `GET /kite/login` | Redirect to manual Zerodha login |
| `GET /kite/callback` | Validate login state and exchange request token |
| `GET /api/connection` | Configuration and local session availability; no remote validation |
| `GET /api/market/snapshot` | Fetch three index quotes; successful retrieval confirms connection |
| `GET /api/market/live` | In-memory ticks, per-instrument freshness and feed status |
| `GET /api/stream/status` | Safe feed connectivity and lifecycle timestamps |
| `GET /api/market/structure` | Spot structure, futures VWAP/basis and EMA state |
| `GET /api/candles/{symbol}?interval=5m&limit=100` | Completed history plus any developing candle |

Snapshots contain `connection_status`, `last_updated` (UTC retrieval timestamp), `partial`, and an `instruments` array with `symbol`, `name`, `value`, `available`, and `quote_timestamp` when supplied by Kite. The dashboard displays retrieval time in IST. Missing quotes are null/Unavailable, never fabricated. Outside market hours, latest quotes may be from the previous session.

Errors use `{ "error": { "code": "...", "message": "..." } }` for application failures. Missing/expired sessions return 401, invalid callbacks 400, unconfigured credentials 503, and upstream failures 502. Failed refreshes clear displayed values. Native routing errors retain FastAPI's standard response format.

## Security and session lifecycle

Access tokens stay in local server memory only. No credential files, cookies, browser responses, or logs contain access tokens. Restarting loses the session. Tokens are conservatively expired at the next 06:00 IST boundary and cleared immediately when Kite returns a token error. There is no automatic reauthentication. A locally available session can still be revoked upstream; snapshot retrieval detects this.

Structured application logs contain only fixed event names, timestamp, severity and status. Exception text, request query strings, upstream payloads and secrets are excluded. SDK debug logging is disabled. Start with `--no-access-log` so the callback request token is not recorded by Uvicorn. Do not enable HTTP wire logging or add a proxy that logs callback query strings.

Authentication uses a short-lived, single-use random state bound to an HttpOnly SameSite cookie via Kite's `redirect_params`. Responses disable caching and referrer forwarding. The API key necessarily appears in the official login URL; the API secret and access token never do.

## Phase 3 architecture

```text
KiteTicker → normalized Tick → MarketState subscriber queue
                                      ↓
                               MarketEngine worker
                                ↙             ↘
                       CandleAggregator       futures VWAP
                              ↓
                    completed candles → SQLite
                              ↑
                     historical recovery worker
                              ↓
                 structure / candles APIs → dashboard
```

`streaming/instruments.py` resolves the three NSE indices and nearest unexpired NFO FUT contracts by underlying, segment, instrument type and expiry. No futures symbols, tokens or expiry dates are hardcoded. Metadata is cached per exchange/day. `KiteStream` checks the resolution period daily and at 15:30 IST; on expiry day, the contract remains eligible until 15:30, then moves to the next expiry. Replacement uses the existing single-connection teardown/generation guards. Historical storage always uses the **actual contract symbol**, never a continuous alias that could mix expiries.

`streaming/market_state.py` publishes immutable internal `Tick` events. The model supports `symbol`, `timestamp`, last price, cumulative session volume, OI, OHLC and exchange average traded price where provided. Index volume/OI are not inferred. Candle code has no dependency on KiteTicker. Its bounded queue has 20,000 slots; overflow marks developing candles partial and is visible in structure status. Future consumers can subscribe separately.

`analytics/session.py` uses `ZoneInfo("Asia/Kolkata")`; the `tzdata` dependency supports Windows. Standard sessions are 09:15–15:30 IST, Monday–Friday. No candles are invented on a holiday: historical bars are authoritative session evidence. Previous-session levels select the latest actual daily bar before today within a 45-calendar-day lookback, not simply yesterday. Special weekend/evening sessions are outside this phase's standard session schedule.

`analytics/candles.py` builds 1m, 5m, 15m and 30m candles anchored to 09:15. For example, the first 30m candle is 09:15–09:45; the last 30m bucket is truncated to 15:15–15:30. Exchange timestamps are used when available, otherwise receipt timestamps. SDK naive exchange timestamps are interpreted using the host timezone that its parser used. Future-dated timestamps beyond five seconds are rejected.

A five-second lateness watermark accepts modest reordering and sets open/close by event time. Older ticks are rejected; historical recovery repairs gaps instead of rewriting closed candles from late ticks. Identical instrument/timestamp/price/volume/OHLC snapshots are deduplicated with a bounded recent cache. Without a broker event ID, identical snapshots in the same exchange second cannot be distinguished from retransmits; `tick_count` counts accepted updates, not exchange trades. No-data intervals remain absent. In-progress bars are never stored. Mid-bucket starts, missing minute coverage and known feed gaps are marked `partial` and excluded from EMA/opening-range calculations until repaired.

Live cumulative volume must have a valid baseline; the first observed cumulative value is **not** allocated to the current candle. Deltas that cross candle boundaries cannot be assigned precisely, so affected live candle volume remains null. Historical futures bars supply their authoritative interval volume later. Index candle volume is always null. Historical tick counts are unknown and remain null, not zero. Candles expose `completed`, `partial`, and `source` in addition to the requested price/time/volume fields.

`analytics/storage.py` uses SQLite WAL and a `(symbol, interval, candle_start)` primary key. It persists completed candles and a small previous-session-level cache, never raw ticks or credentials. Historical corrections upsert existing bars without duplicates; partial imports cannot downgrade a complete candle. The default database is `data/market.sqlite3`; override with `MARKET_DB_PATH` in `.env` if needed. Database files and `.env` are Git-ignored. Keep the database on a local nonsynced drive when possible; do not share a live WAL database between processes.

`analytics/history.py` restores history from SQLite immediately through the aggregator query interface and fetches missing context in a background worker after login. Initial warm-up requests up to ten calendar days of 1m history for the current symbols; subsequent passes fetch missing/partial session coverage, aligned to 30m boundaries. Full-session and previous-level caches avoid redundant completed-history requests. Calls are paced at least 0.4 seconds apart, checked about every 60 seconds, and use the existing SDK timeout. Completed historical minutes seed a developing larger candle without inventing the missing portion of the current minute. Missing history remains unavailable and is retried. Actual candle history, not assumed holiday dates, controls recovery and EMA warm-up.

`analytics/indicators.py` implements SMA-seeded EMA9/EMA20 from completed, nonpartial **spot** candles for 5m/15m/30m. At least 9/20 eligible bars are required. Comparison flags use the last completed spot candle close (exposed as `price_basis`), not the developing candle. No trading decisions are produced.

Futures VWAP uses cumulative traded volume correctly. When Kite supplies valid exchange average traded price plus nonzero volume, `vwap_method=exchange_session_average` represents the full session and survives a mid-session login. Without that field, the engine computes `sum(last_price × positive_volume_delta) / sum(positive_volume_delta)` after establishing a baseline. This is a **sampled observed-window estimate**, labelled `observed_volume_deltas` and `vwap_full_session=false`, not an exact reconstruction of the morning. Historical OHLC cannot reconstruct exact VWAP, so it is never used as a substitute. Missing/zero volume and counter resets make VWAP unavailable until valid data resumes. Spot volume is never used for VWAP.

Opening range requires all 15 complete 1m spot candles from 09:15 through 09:29. If any are missing/partial, OR fields remain null. Previous high/low/close are cached per symbol/as-of session date. Day OHLC prefers the supplied spot snapshot; complete available history can supply fallback levels. Futures basis is current future minus current spot; it is not an arbitrage signal. Staleness and recovery status remain visible.

## Endpoints and symbols

`GET /api/market/snapshot` retains its original three-index REST contract. The streaming live endpoint now also includes the two discovered futures.

`GET /api/market/structure` returns `nifty`, `banknifty`, `NIFTY_FUT`, `BANKNIFTY_FUT`, session/as-of timestamps, history-recovery status and dropped tick-event count. NIFTY/BANKNIFTY sections include spot/future/basis, day levels, previous session/date, opening range/state, futures VWAP/source/coverage, stale flag, and `5m`/`15m`/`30m` EMA fields. Null means unavailable or warming up.

`GET /api/candles/NIFTY?interval=5m&limit=100` returns chronological candles. Intervals: `1m`, `5m`, `15m`, `30m`; limits: 1–1000. Supported aliases: `NIFTY`, `BANKNIFTY`, `NIFTY_FUT`, `BANKNIFTY_FUT`; spot names `NIFTY 50`, `NIFTY BANK`, `INDIA VIX` and currently subscribed actual futures symbols are also accepted. Futures aliases resolve to the current actual contract. Invalid interval/limit returns 422; unknown/unresolved symbol returns 404. Inspect `completed` and `partial` before consuming a candle.

The dashboard retains live feed status and adds two Market Structure panels. It labels futures VWAP separately from spot day/OR/EMA metrics, shows source and stale state, and clears panels on request failure. Structure refreshes every five seconds.

## Verification and live acceptance

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/dashboard.test.cjs tests/structure.test.cjs tests/options.test.cjs
```

Tests mock all Zerodha calls and isolate SQLite under temporary directories. They cover the original Phase 1/2 behavior, all candle intervals, boundaries, lateness, duplicates, partial data, futures discovery/rollover, volume/VWAP, opening range, previous-session caching, EMAs, database uniqueness/reopen, recovery and API validation. Node tests use the built-in runner; no npm packages are needed. An existing Starlette/httpx deprecation warning does not affect passing tests.

For Phase 3 live acceptance, restart the single-worker application using the same startup command and log in manually. Check `/api/market/live` lists five instruments, `/api/market/structure` progresses through history recovery, and candles/EMA values warm up. Verify the futures contract/expiry, opening range and previous levels against Kite. History entitlement or temporary API errors may leave metrics unavailable; authentication and live streaming remain independently visible. The previously running Phase 2 server is not automatically restarted by development changes.

Phase 4 extends this architecture below. Phase 5.1 adds persistence only; no trade execution or CALL/PUT recommendation is implemented.

References: [authentication](https://kite.trade/docs/connect/v3/user/), [WebSocket packet fields](https://kite.trade/docs/connect/v3/websocket/), [historical data](https://kite.trade/docs/connect/v3/historical/), [instrument metadata](https://kite.trade/docs/connect/v3/market-quotes/), [official Python SDK](https://github.com/zerodha/pykiteconnect).


## Phase 4 options architecture

The existing single KiteTicker connection still owns the five Phase 3 instruments. `OptionsService` adds a separate normalized options path and three bounded background workers (tick processing, REST refresh, previous-close OI warm-up). Options never enter the candle aggregator. Failures stay in the options layer; token rejection invalidates only the matching authentication session.

- `options/discovery.py` reads cached NFO metadata for index, expiry, strike, CE/PE, lot size, token and symbol. It exposes `get_expiries`, `get_option_contract`, actual-strike ATM selection and expiry selections. Nearest/next use sorted eligible listed expiries, with settlement at 15:30 IST. Monthly is reported only when a listed option expiry matches an actual futures expiry. No expiry weekday is assumed. Metadata is refreshed every 15 minutes and on session/day changes; cached expired contracts are filtered immediately at settlement.
- The **live layer** subscribes to the nearest expiry, ATM ±10 available strikes, CE+PE, for each index. One strike-step movement is tolerated; two steps shift the window using subscribe/unsubscribe differences. Duplicate sets are ignored. Reconnect replays the desired base and option subscriptions and removes obsolete tokens. Normalization captures LTP, OI, cumulative volume, top bid/ask quantities, depth and exchange timestamp. The 10,000-item queue never blocks the base feed; dropped events are counted in responses.
- The **full-expiry layer** uses REST quote batches of at most 200 instruments every 20 seconds by default. One extra explicitly requested expiry per index may be refreshed alongside nearest. Snapshots are replaced as a batch, including missing contracts: old missing quotes are not carried forward to pretend coverage. Batch start/end times expose that a multi-request snapshot is not atomic. A missing quote leaves full-chain calculations unavailable.
- `kite_client.py` shares quote/historical request budgets across phases: quote requests are spaced by at least 1.1 seconds, historical by 0.4 seconds. These apply to this single application process/API key; other applications using the same key must budget their traffic separately. No new SDK or numerical-library dependency is required.
- `options/state.py` separates normalized contract metadata from market observations. Each session/day starts with a fresh first-valid-observation baseline, established during standard market hours. `price_change`, `volume_change`, `oi_change_session`/`oi_change_intraday`, and `oi_change_percent` use that observation baseline. They are not changes from the previous close. Zero-baseline percentages are null. A quote predating the baseline cannot produce a session change.
- Previous-close OI uses derivative daily history with `oi=True`, matched to the actual previous session date established by Phase 3. An older available OI record is never substituted. Values (including unavailable/null) are cached in SQLite by actual symbol and as-of date. A paced worker prioritizes ATM and alternates indices; remaining chains warm up gradually. Failures back off per contract instead of blocking all other baselines. `oi_change_vs_previous_close` and `previous_oi_session_date` stay separate from session changes.
- `options/analytics.py` produces descriptive LONG_BUILDUP, SHORT_BUILDUP, SHORT_COVERING, LONG_UNWINDING, NEUTRAL or UNAVAILABLE labels. Price/OI percentage thresholds are configurable. Writing zones require price decline plus OI addition, rather than assuming every OI addition is writing. Walls and top-three rankings are levels, not guaranteed support/resistance. Additions/unwinding consistently use session-baseline OI changes.
- Full-expiry OI/volume PCR, OI walls and max pain use **only the full REST snapshot**, never a mixture of live and REST observations. Full coverage and the necessary field availability are required; incomplete chains return null/empty aggregate results. Near-ATM PCR is separately labelled and requires complete coverage of that window. Max pain minimizes OI-weighted settlement intrinsic payout over listed strikes and is secondary descriptive information. Ties select the lower listed strike.
- `options/pricing.py` uses European Black-76 when a fresh same-expiry futures quote exists. It never substitutes a different-expiry future for that purpose. Otherwise it uses Black-Scholes with an explicit zero-dividend spot assumption. These are locally calculated model estimates, not exchange-provided Greeks. Prices use a valid bid/ask midpoint, falling back to LTP. Time is ACT/365 to the listed expiry's 15:30 IST. IV uses bounded bisection (0.01%–500% annual volatility); expired, nonfinite, nonpositive, arbitrage-inconsistent or out-of-bracket prices return null. No API contains NaN/Infinity. `iv` is annual decimal volatility, delta is per unit of the labelled model underlying, gamma per underlying point, theta per calendar day, and vega per 1 percentage point of volatility. The zero-dividend fallback and snapshot timing can differ from broker platform values.
- Liquidity states LIQUID/MODERATE/POOR use midpoint-relative spread percentage, OI, volume and available top quantities. Missing, zero or crossed quotes are POOR with unavailable spread; this is a data-quality label, not a trade instruction.
- `options/storage.py` shares the SQLite file using its own connection. Once per minute per index/expiry, it stores summary values and ATM ±5 strike details with a uniqueness key. It does not persist raw option ticks. Stored records retain coverage/staleness and clearly named OI changes. The options database remains under the existing ignored database path.

### Options configuration

Optional `.env` variables (defaults shown in `.env.example`):

| Variable | Default |
| --- | --- |
| `OPTIONS_RISK_FREE_RATE` | `0.06` annual decimal |
| `OPTIONS_REFRESH_SECONDS` | `20` (minimum 15) |
| `OPTIONS_STALE_SECONDS` | `60` |
| `OPTIONS_PRICE_CHANGE_PERCENT` | `0.5` |
| `OPTIONS_OI_CHANGE_PERCENT` | `1` |
| `OPTIONS_LIQUID_SPREAD_PERCENT` | `1` |
| `OPTIONS_MODERATE_SPREAD_PERCENT` | `3` |
| `OPTIONS_LIQUID_MIN_OI` / `OPTIONS_LIQUID_MIN_VOLUME` | `1000` / `100` |
| `OPTIONS_MODERATE_MIN_OI` / `OPTIONS_MODERATE_MIN_VOLUME` | `100` / `10` |

The 6% rate is a configurable model assumption, not a current market-rate claim. Restart after changing configuration. A stale spot removes ATM/spot-derived Greeks rather than inventing a current underlying value. After hours, quotes/Greeks can be unavailable or stale without indicating an application failure.

### Options API

- `GET /api/options/nifty?window=10`
- `GET /api/options/banknifty?expiry=YYYY-MM-DD&window=10`
- `GET /api/options/nifty/chain?expiry=YYYY-MM-DD&strike_min=23000&strike_max=24000&limit=100&offset=0`
- `GET /api/options/banknifty/chain`

Summary `window` is 1–30 and controls the labelled near-ATM PCR window; it does not change the stable nearest-expiry live subscription policy. An explicit listed expiry schedules a background REST refresh; its first response may be pending/incomplete. The default remains nearest. Unlisted expiries return 404, invalid dates/ranges/limits return 422. Chain pagination allows 1–500 rows (default 200), offset 0–10000; filtering does not redefine full-chain coverage.

Every response exposes `last_stream_tick_at`, `last_full_chain_refresh_at`, `stale`, and coverage (expected, received/fresh, percentage, OI and volume availability). The summary also includes snapshot start time, selections, PCR scopes, OI-change definition, matched-expiry futures, front-month futures, ATM detail and model source. A missing expiry-matched future is null even if a front-month future exists. Chain rows and ATM details can overlay newer live observations and carry their own source/timestamps/stale flags; **aggregate metrics and coverage remain based on the full REST snapshot**. Previous-close OI may warm up later than quotes.

### Phase 4 live acceptance checklist

1. Run all Python and frontend tests above, then restart the single-worker app and manually log in.
2. Confirm the Phase 1 REST snapshot and Phase 2/3 feed, candles and structure still work.
3. Verify listed expiries, ATM strikes, CE/PE symbols and lots against Kite metadata; do not assume a weekly expiry exists for either index.
4. Confirm the options panels show both indices, full-chain coverage and last refresh times. Allow the first REST refresh and historical OI warm-up to complete.
5. Compare ATM prices/OI, full-expiry PCR and OI walls with the same expiry/coverage on the broker platform. Check session ΔOI and previous-close ΔOI are labelled separately.
6. Check the IV model source: matched-expiry future/Black-76, or spot/Black-Scholes zero-dividend fallback. Different quotes, times, rates or dividend assumptions produce different IV/Greeks.
7. During underlying movement, verify the subscription window shifts after the configured two-strike hysteresis, using the same WebSocket connection. Reconnect should restore the latest desired window.
8. Verify missing quotes/stale data suppress full-chain aggregates; market-closed status is not a trading recommendation.
9. Confirm `option_snapshots` grows at minute cadence and `option_previous_oi` caches historical values. No credentials or raw ticks should appear in the database.

Phase 4 real-account acceptance has not been run by these mock tests. The existing server is not automatically restarted. Phase 5.3 below adds descriptive directional decisions; no order placement or automatic trading is implemented.

### Phase 5.1 full-chain history

`option_chain_snapshots` now stores every discovered CE/PE contract in the nearest listed expiry for NIFTY and BANKNIFTY once per minute, alongside the unchanged `option_snapshots` summary/ATM ±5 records. Other expiries requested through the dashboard are not archived. Schema and indexes are created automatically on startup in the existing ignored SQLite database; no configuration change is needed.

The first persistence pass in each IST minute captures the full available chain after REST refresh, including fresh stream overlays, even without a fresh spot/ATM. This is a sampled observation, not a minute-close bar. Missing fields stay NULL; nonfinite numbers are converted to NULL. Each row contains contract identity, prices/top quotes, quantities, spreads, OI and volume changes, locally calculated IV/Greeks, liquidity/positioning, source, quote timestamp and stale flag. `timestamp` is capture time; `quote_timestamp` is the supplied exchange time and may be NULL. Mixed quote times and missing/stale observations remain explicit rather than being filled from older snapshots.

An atomic transaction and unique `(index_name, expiry, snapshot_minute, strike, option_type)` key protect retries/restarts. The first saved observation is immutable for that minute. Indexes support whole-chain retrieval, contract history and cross-index time ranges. No raw ticks are persisted and no signals are generated. Full-chain history increases database size; this phase does not automatically delete historical records.

Test with `py -m pytest -q --basetemp=.pytest_tmp` (or `.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp` when the Windows launcher has no registered Python).

Additional references: [Kite request limits](https://kite.trade/docs/connect/v3/exceptions/), [quote batch fields](https://kite.trade/docs/connect/v3/market-quotes/), [CME options analytics](https://www.cmegroup.com/market-data/greeks-and-implied-volatility-data.html).

### Phase 5.2 deterministic scoring foundation

`app.signals` exposes `SignalInput`, `SignalConfig`, `CategoryScore`, `ScoreResult` and `SignalEngine`. This is a pure, in-process library: it does not read clocks, databases, HTTP or WebSocket state. Supply one index's Phase 3 structure entry and Phase 4 options summary, plus normalized India VIX data. No lifecycle, targets, stops, endpoints, dashboard integration or CALL/PUT output is implemented.

```python
from app.signals import SignalEngine, SignalInput

snapshot = SignalInput(
    index_name="NIFTY",  # or BANKNIFTY
    as_of="2026-09-25T10:00:00+05:30",
    structure=structure_snapshot["nifty"],
    options=options_summary,
    volatility={"level": 18.0, "change_percent": 2.0, "stale": False},
)
result = SignalEngine().score(snapshot)
```

The caller selects contemporaneous observations and marks stale sources; replay never consults today's clock or future observations. Missing/nonfinite values are unavailable. Preserve the supplied snapshot with its `as_of`, result version and configuration when replaying; Phase 5.1 chain rows alone do not contain all required price/VIX context. Structure `stale=True` suppresses price and futures scoring, options `stale=True` suppresses options scoring, and stale VIX is unavailable. EMA partial/stale flags are respected when supplied.

Default category budgets are price/trend 30, options/positioning 30, breadth 15, volatility 10, futures/structure 15. Breadth is reserved and returns unavailable with zero available weight. Available weight counts observed scoring components, including neutral observations; missing components are never redistributed or scaled to 100. Bullish and bearish points are separate descriptive evidence totals, not confidence probabilities. Each category reports direction, evidence and contradictions. A net score within 10% of its available weight is neutral. The result intentionally has no overall directional recommendation.

- Price allocates 25% of its budget to opening-range breakout, 20% previous-day range, 15% previous close, 10% current day-range position, and 30% shared EMA9/EMA20 consensus across 5m/15m/30m. EMA intervals cannot each earn a full trend budget. Missing intervals reduce coverage. Numeric comparisons use a configurable 0.05% deadband; day range upper/lower thresholds default to 80%/20%.
- Options allocates 35% to writing/unwinding, 30% liquid ATM CE/PE behavior, 15% OI-wall breakout, and 20% PCR confirmation. Correlated OI observations share a budget; OI additions alone cannot identify writers. Ratios (full OI, full volume, near-ATM OI) above 1.2/below 0.8 confirm existing non-PCR evidence only; disagreement is recorded. PCR alone cannot establish or reverse direction. ATM data with poor/unknown liquidity or invalid supplied IV is excluded. Missing IV does not invalidate otherwise liquid positioning. Max pain is secondary context with no points.
- Futures uses 60% for future versus VWAP and 40% for agreeing spot/future moves. Optional `future_change_percent` and `spot_change_percent` must use the same reference period; Phase 3 does not currently supply these, so this component stays unavailable unless the caller supplies both. Opposing moves are flagged. Basis is context only and cannot determine direction.
- VIX level supplies neutral risk context (low at/below 12, elevated at/above 25) and optional intraday change indicates rising/falling outside ±3%. It awards no bullish/bearish points; VIX rising never automatically implies bearish index direction.

All category weights, component fractions and thresholds live in `SignalConfig`, which validates finite values and budgets. These are explicit initial heuristics, not calibrated or backtested trading rules. Tests cover both indices, opposing/neutral evidence, replay repeatability, coverage, bounds, staleness and nonfinite inputs.

### Phase 5.3 live breadth and directional decisions

`BreadthService` resolves NSE equity tokens through the existing instrument metadata cache and subscribes through the **same KiteTicker connection**. Equity ticks use a separate bounded queue, so the original index/futures state, option subscriptions and candle APIs retain their existing behavior. Constituents are deduplicated across indices and replayed on reconnect. Login/day changes reset breadth state; subscription discovery failures retry after five minutes. No credentials, raw ticks or extra breadth tables are persisted.

Membership is loaded once per session/day from the current constituent CSV downloads on the official [NIFTY 50](https://www.niftyindices.com/indices/equity/broad-based-indices/nifty--50) and [Nifty Bank](https://www.niftyindices.com/indices/equity/sectoral-indices/nifty-bank) pages. Failed NIFTY discovery is unavailable. Failed bank discovery tracks HDFCBANK, ICICIBANK, SBIN, AXISBANK and KOTAKBANK as a clearly labelled partial proxy; it cannot authorize a decision. Tokens are never hardcoded. Metadata misses remain in coverage denominators.

Optionally set `BREADTH_CONSTITUENTS_FILE=data/constituents.json` to supply dated membership and weights. Each index entry has `as_of` (current IST date), `symbols` (full symbol list), `full_index` (true only for the complete index), and optional `weights` keyed by symbol. For example, a **partial proxy**, not full index configuration:

```json
{
  "BANKNIFTY": {
    "as_of": "2026-09-25",
    "symbols": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
    "full_index": false,
    "weights": {}
  }
}
```

A configured file replaces both automatic lists; supply both entries for full operation. Missing/stale entries fail closed. Complete positive finite weights enable weighted percentages; missing/partial/invalid weights explicitly fall back to unweighted breadth. A/D counts and the A/D ratio always remain counts. Zero declines produce a NULL ratio, never Infinity. Percentages use fresh observed constituents; count and weight coverage against the entire membership are reported separately. Unchanged is distinct from missing or stale.

Breadth includes advances, declines, unchanged, positive/negative percentages, and percent above 5m/15m EMA20 where available. Each EMA needs 20 contiguous completed nonpartial live bars. There is no additional historical fetch; warm-up restarts after a gap/session reset, and unavailable EMAs stay NULL. EMA coverage is reported separately. Constituent data becomes stale at 30 seconds. Snapshot calculations use normalized events and can also be replayed through the pure `breadth.metrics.calculate` function with historical membership/weights; never use today's constituents for an old replay.

The 15-point breadth category allocates 60% to positive/negative participation and 20% to each EMA measure. Default directional participation is at least 60%; EMA measures require 90% constituent coverage before scoring. VIX remains non-directional, so the four aligned categories normally required are price, options, breadth and futures. The runtime adapter now derives spot/future changes from their respective previous closes.

`SignalEngine().decide(snapshot)` returns a `DecisionResult` with `decision`, `confidence`, bullish/bearish scores, aligned category names, category scores, evidence, contradictions and data quality. Use `dataclasses.asdict(result)` for JSON serialization. CALL/PUT requires all of:

- Winning score at least **70** on the original 100-point budget, at least **4** aligned categories, and separation at least **15**.
- Fresh structure, full-chain options refresh, breadth and VIX. Each requires an explicit `stale=False` and aware timestamp no later than `SignalInput.as_of` and no older than 60 seconds. Structure, breadth and VIX use `as_of`; options uses `last_full_chain_refresh_at`. Missing critical spot/future/VIX values also block.
- At least **95%** options quote coverage (computed from expected/received counts), and at least **90%** full-index breadth coverage. Existing Phase 4 stale flags can impose stricter full-chain requirements. A proxy cannot pass.
- No major category contradiction: an opposing category direction, at least 25% opposing evidence within a major category, or explicit spot/future divergence blocks the result.

Anything else returns **NO_TRADE**, with blocking reasons and confidence zero. Confidence for a passing decision is the winning score out of 100, **not a calibrated success probability**. Missing scores are not redistributed. Thresholds remain in `SignalConfig`; results include the configuration and scoring version `5.3.1` for replay. The Phase 5.2 `score()` interface remains available.

Production wiring starts breadth after authentication through the existing app lifecycle. Internal callers can use `app.state.breadth.snapshot("NIFTY")` and `app.state.signals.decision("NIFTY")` during lifespan; equivalent BANKNIFTY calls are supported. No new API or dashboard is added, and there is no signal lifecycle, entry, stop, target or execution. Restart is required to activate this code; mock tests do not constitute live market acceptance.

### Phase 5.4 structural plans and signal lifecycle

This phase extends the internal Phase 5.3 decisions with `SignalPlanner`, `ExecutionConfig` and `SignalLifecycle`. There are still **no order APIs or broker execution**. Entry, invalidation, T1/T2 and risk/reward are expressed in the named underlying's price, never in option premium. Confirmation and target states record observed conditions; they are not orders, fills or realized profit.

The planner first requires an existing qualifying CALL/PUT decision. It chooses the closest known opening-range, previous-day or confirmed swing barrier for an index breakout/breakdown; an already-broken barrier requires a retest. An OI wall is eligible only when present in the full-chain top-OI rankings with positive OI, and still needs price confirmation. Recent swings use a completed five-bar pivot with two completed bars on each side; partial/gapped/future bars cannot establish a pivot. If no spot barrier exists, supplied futures structure can support a VWAP reclaim/loss, entirely in that futures contract's price space.

Invalidation is the nearest structural support below a CALL trigger or resistance above a PUT trigger. T1/T2 are the nearest known structural levels ahead in the direction, including day extremes. The planner does not skip a nearer obstacle to improve R:R or invent a target. T2 may be NULL; missing invalidation/T1, already invalidated/exhausted structure, or T1 R:R below **1.5** returns NO_TRADE without an actionable plan. Levels are frozen when the candidate is created.

Contract selection receives the **complete, unpaginated metadata/quote chain** for the nearest selected expiry after direction and structure qualify. It tries ATM first, then only the adjacent lower strike CE for CALL or adjacent higher strike PE for PUT. It never ranks by cheap premium or falls back to far OTM. A contract needs unique identity, fresh receipt/exchange timestamps, valid top quotes and quantities, finite positive LTP, acceptable midpoint-relative spread, adequate OI/volume and LIQUID/MODERATE status. Defaults: quote age at most 30 seconds, spread at most 1%, OI at least 1,000 and volume at least 100. Missing fields fail selection.

Lifecycle behavior:

| State | Meaning |
| --- | --- |
| CANDIDATE | Qualified structural plan and selected liquid option; waiting for confirmation |
| CONFIRMED | A new complete nonpartial 5m candle crossed the trigger, or touched and closed back beyond it on a retest; qualification/liquidity/R:R rechecked |
| INVALIDATED | Structural invalidation touched before confirmation, or the selected contract/R:R failed confirmation checks |
| TARGET1_HIT | Underlying T1 observed after confirmation; continues to T2, stop or session expiry |
| TARGET2_HIT | Underlying T2 observed; terminal |
| STOPPED | Structural invalidation touched after confirmation; terminal |
| EXPIRED | Candidate lifetime/cutoff or session end reached; terminal |

Confirmation candles must start after candidate creation, belong to the trigger instrument, and finish by the supplied observation time. A breakout must open on the unbroken side and close beyond the level; a retest opens on the broken side, touches the level, then closes back beyond it. Timestamps are explicit and aware, so replay does not consult a wall clock. Partial/stale/wrong-instrument candles cannot confirm. Confirmation rechecks the original option's ATM/ITM eligibility and calculates R:R using the worse of the candle close/current underlying observation, preventing a gap or late observation from retaining an obsolete trigger-price R:R. No target is credited from the confirmation candle itself. A later bar touching both invalidation and target is conservatively stop-first because ordering is unknown. Stale data cannot confirm or hit levels; expiry still applies. Out-of-order observations and already-consumed candles cannot mutate state.

Candidates last **900 seconds**, capped at **15:00 IST**. Creation and confirmation at/after the cutoff are rejected; existing confirmed signals can be observed until 15:30 IST, when they expire. Weekends/pre-open creation is rejected. These clock checks are not an exchange-holiday calendar; the existing freshness/coverage gates remain required. A signal with no T2 stays at TARGET1_HIT until stop or session expiry.

Only one active signal per index is allowed. A deterministic ID deduplicates the same index/session/direction/instrument/structural source/level even if quote times or targets change. The `signal_lifecycle` SQLite table stores plans and transition history in the existing ignored market database, preserving duplicate suppression across restarts. Terminal setups cannot be recreated that session. Use the existing **single application worker**; the lifecycle lock serializes callers in that process. Replay should use an isolated journal and the same scoring/execution configuration. No raw ticks, credentials or broker responses are stored in this journal.

Internal usage:

```python
from app.signals import SignalPlanner, SignalLifecycle

planner = SignalPlanner()  # Pure supplied-snapshot planning, default configuration
result = planner.build(snapshot, complete_chain_rows)

lifecycle = SignalLifecycle(planner)  # In-memory SQLite journal for replay by default
submission = lifecycle.submit(snapshot, complete_chain_rows)
if submission.record:
    record = lifecycle.advance(
        submission.record.signal_id, next_snapshot, next_chain_rows,
        bar=completed_5m_bar,  # Existing Phase 3 bar schema, including symbol/interval
    )
lifecycle.close()
```

Within the running app, `app.state.signals.evaluate("NIFTY")` (or `"BANKNIFTY"`) collects existing structure, full options rows and completed candles, then creates or advances the signal. `submission.created` distinguishes a new candidate from an existing record; duplicates return NO_TRADE with the existing record and reason. Evaluation is explicit and synchronous, not a new background loop, API or dashboard feature. The old `decision()` method remains read-only scoring. Lifecycles are expired on the next evaluation/advance; no timers or orders run independently.

Optional `.env` settings, read at startup (restart required):

```dotenv
SIGNAL_MIN_T1_RR=1.5
SIGNAL_CANDIDATE_LIFETIME_SECONDS=900
SIGNAL_NEW_ENTRY_CUTOFF=15:00
SIGNAL_OPTION_QUOTE_MAX_AGE_SECONDS=30
SIGNAL_OPTION_MAX_SPREAD_PERCENT=1
SIGNAL_OPTION_MIN_OI=1000
SIGNAL_OPTION_MIN_VOLUME=100
```

Mock tests cover structural triggers, confirmation, invalidation, R:R, both option directions, illiquidity, targets/stops, duplicate persistence, expiry and cutoff. Live market acceptance remains pending; the app is not automatically restarted.

### Phase 5.5 explainability, outcomes, APIs and dashboard

Phase 5.5 completes the runtime integration described in the earlier phases. A single `signal-observer` worker evaluates NIFTY and BANKNIFTY every two seconds using the existing normalized market state, structure, options and breadth services. It performs no broker requests and opens no additional WebSocket. It creates candidates and advances existing lifecycles without requiring an open dashboard. Shutdown stops the observer before closing its input services. Run one Uvicorn worker, as required for the existing connection/session owner.

The deterministic engine and planner still consume supplied snapshots; the worker is only their runtime adapter. Candidate creation uses only data available at that observation time. Future/partial candles cannot establish a swing or confirm a candidate. Changing later prices never rewrites the original explanation.

SQLite schema is created automatically in the existing ignored market database:

| Table | Purpose |
| --- | --- |
| `signals` | Indexed current record: ID, index, direction, created/updated times, state and frozen plan |
| `signal_events` | Append-only, uniquely sequenced CANDIDATE, CONFIRMED and all subsequent state changes, each with its observation-time explanation |
| `signal_outcomes` | Latest observed outcome metrics per signal |
| `signal_lifecycle` | Existing Phase 5.4 restart/deduplication journal, retained for compatibility |

Each state transition and its latest record/outcome are saved in one SQLite transaction. Explanations include bullish/bearish and category scores, evidence, contradictions, data quality, the supplied price/futures structure, options summary, VIX and breadth, scoring/execution configuration and version, and the confirmation candle where applicable. Relevant chain rows include ATM ±2 strikes and the reported OI walls; the frozen plan includes the selected contract, trigger, invalidation and targets. This context is labelled as a subset; full-chain aggregates are preserved in the options summary and Phase 5.1 continues its independent full-chain minute archive. Replay can reproduce scoring from the captured snapshot/configuration without consulting subsequent data or today's constituents.

Legacy Phase 5.4 signals remain visible, but missing original evidence or outcome entries are explicitly unavailable; this migration never invents past snapshots or fills. Event explanations are immutable even when later records advance. No NO_TRADE rows are written, and optional periodic diagnostic persistence is not enabled. Finite JSON guards replace nonfinite values with NULL, and sensitive credential/authorization/URL fields are excluded from captured context.

Outcome tracking starts only at CONFIRMED and records:

- Observed entry time, conservative entry underlying price, and the fresh selected option LTP when available.
- T1/T2/stop hit flags, observation timestamps, seconds to each event, and duration since confirmation.
- MFE and MAE in underlying points and R units, using direction-normalized excursions against the frozen invalidation risk.
- Terminal model result in R, exit time and underlying reference price. T2 uses the target level; stops use the worse of the structural stop/current observation for gaps. Expiry uses the latest fresh observed underlying only if there is no detected observation gap; otherwise result R remains NULL. Open signals do not claim a realized R result.

These are **sampled observations and model outcomes, not broker fills, executable quotes or option P&L**. The two-second worker plus eligible completed bars can miss intrabar excursions; intervals over 30 seconds between observations set `observation_gap`. Hit timestamps are detection times, not invented exchange execution times. Bars touching both stop and target are stop-first and cannot add favorable excursion credit. Existing confirmed signals resume observation after restart; gaps remain explicit, and no future backfill changes their original entry or evidence.

Read-only APIs (GET requests never create or advance signals):

| Endpoint | Response |
| --- | --- |
| `/api/signals/nifty` | Latest NIFTY panel result; normal NO_TRADE while waiting or blocked |
| `/api/signals/banknifty` | Latest BANKNIFTY panel result |
| `/api/signals/current` | Both results in a `signals` list |
| `/api/signals/history` | `items`, `total`, `limit`, `offset`; newest creation first, stable ID tie-break |
| `/api/signals/{signal_id}` | Latest `record`, ordered `events`, immutable `creation_explanation`; unknown ID is 404 |

History filters: `index=nifty|banknifty`, `direction=CALL|PUT`, `state` (one of the seven lifecycle states), and inclusive `date_from`/`date_to` in `YYYY-MM-DD` on IST creation date. Pagination uses `limit` 1–100 (default 25) and `offset` 0–100000. Invalid filters return 422. For example: `/api/signals/history?index=nifty&state=CONFIRMED&limit=25&offset=0`. State filters match the latest state; transition history remains in the detail response.

Dashboard panels refresh every five seconds and show direction/NO TRADE, state, confidence, scores/category breakdown, structural plan/R:R, contract and selection-time liquidity, evidence, contradictions, quality and update time. Active signals show their creation scores/evidence; `current_qualification` separately reports present scoring, while data quality always reflects the latest observation. Terminal or blocked setups show NO TRADE normally. A worker result older than ten seconds is suppressed as NO_TRADE/waiting, and frontend fetch failures clear prior actionable presentation. Outcome details are also shown when entry was confirmed. All text is rendered through `textContent`.

No order placement, automatic execution or broker position management is present. Phase 1–4 endpoints and authentication behavior remain unchanged. `.env`, SQLite files and WAL/SHM files remain ignored. Restart the app to activate this phase; installation/configuration commands above still apply.

#### Phase 5 live acceptance checklist

1. Run `py -m pytest -q --basetemp=.pytest_tmp`. If the Windows launcher reports no installed Python, use `.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp`. Run `node --test tests/dashboard.test.cjs tests/structure.test.cjs tests/options.test.cjs tests/signals.test.cjs` with your Node executable.
2. Restart a single application worker and manually authenticate. Check Phase 1 snapshots, live feed, candles/structure and options panels still work. No username/password/TOTP automation is used.
3. Confirm full constituent discovery, explicit weighted/unweighted breadth, fresh VIX and options coverage. Missing membership, EMA warm-up or incomplete data should produce ordinary NO TRADE with reasons.
4. Compare a candidate's trigger, support/resistance targets and selected ATM/adjacent ITM contract with the captured snapshot. Verify liquidity, nearest expiry, cutoff and minimum R:R checks; do not expect frequent signals.
5. Use `/api/signals/{signal_id}` to verify the immutable CANDIDATE explanation, then a new completed 5m confirmation and the CONFIRMED evidence/outcome entry. Confirm the candidate snapshot did not change afterward.
6. Observe T1/T2/stop or expiry transitions and check timestamps, durations, MFE/MAE and model R definitions. Restart during a test session and check deduplication plus explicit observation gaps; do not treat sampled outcomes as executed returns.
7. Test history pagination/index/state/date filters, API 404/422 handling and neutral NO TRADE presentation. Disconnect the feed and confirm stale quality/expiry behavior without false target hits.
8. Verify no new candidates/confirmations at or after 15:00 IST, pending candidate expiry, and session expiry at 15:30. Confirm there are no broker orders and no secrets in browser responses, logs or tracked files.

Automated tests use mocks and do not replace this real-account live acceptance. The development workflow does not restart the running app automatically.
