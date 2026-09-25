# NIFTY market dashboard — Phase 3

Local, single-user, read-only FastAPI application using the official `kiteconnect` Python SDK and KiteTicker. Displays NIFTY 50 (`NSE:NIFTY 50`), BANK NIFTY (`NSE:NIFTY BANK`) and INDIA VIX (`NSE:INDIA VIX`). Adds front-month NIFTY/BANKNIFTY futures, candles, futures VWAP, spot market structure and completed-candle EMA9/EMA20. No signals, orders, automatic trading, option-chain analytics, RSI or MACD. Phases 1 and 2 were live-tested successfully by the user with their real Zerodha account.

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
node --test tests/dashboard.test.cjs tests/structure.test.cjs
```

Tests mock all Zerodha calls and isolate SQLite under temporary directories. They cover the original Phase 1/2 behavior, all candle intervals, boundaries, lateness, duplicates, partial data, futures discovery/rollover, volume/VWAP, opening range, previous-session caching, EMAs, database uniqueness/reopen, recovery and API validation. Node tests use the built-in runner; no npm packages are needed. An existing Starlette/httpx deprecation warning does not affect passing tests.

For Phase 3 live acceptance, restart the single-worker application using the same startup command and log in manually. Check `/api/market/live` lists five instruments, `/api/market/structure` progresses through history recovery, and candles/EMA values warm up. Verify the futures contract/expiry, opening range and previous levels against Kite. History entitlement or temporary API errors may leave metrics unavailable; authentication and live streaming remain independently visible. The previously running Phase 2 server is not automatically restarted by development changes.

No Phase 4, trade execution, option chain, or CALL/PUT recommendation is implemented.

References: [authentication](https://kite.trade/docs/connect/v3/user/), [WebSocket packet fields](https://kite.trade/docs/connect/v3/websocket/), [historical data](https://kite.trade/docs/connect/v3/historical/), [instrument metadata](https://kite.trade/docs/connect/v3/market-quotes/), [official Python SDK](https://github.com/zerodha/pykiteconnect).
