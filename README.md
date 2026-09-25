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

Phase 4 extends this architecture below. No Phase 5, trade execution or CALL/PUT recommendation is implemented.

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

Phase 4 real-account acceptance has not been run by these mock tests. The existing server is not automatically restarted. No Phase 5, CALL/PUT recommendation, order placement or automatic trading is implemented.

Additional references: [Kite request limits](https://kite.trade/docs/connect/v3/exceptions/), [quote batch fields](https://kite.trade/docs/connect/v3/market-quotes/), [CME options analytics](https://www.cmegroup.com/market-data/greeks-and-implied-volatility-data.html).
