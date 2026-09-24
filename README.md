# NIFTY market dashboard — Phase 2

Local, single-user, read-only FastAPI application using the official `kiteconnect` Python SDK and KiteTicker. Displays NIFTY 50 (`NSE:NIFTY 50`), BANK NIFTY (`NSE:NIFTY BANK`) and INDIA VIX (`NSE:INDIA VIX`). No signals, orders, automatic trading, options analytics, indicators or candle aggregation. Phase 1 was live-tested successfully by the user with their real Zerodha account.

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

Snapshots contain `connection_status`, `last_updated` (UTC retrieval timestamp), `partial`, and an `instruments` array with `symbol`, `name`, `value`, `available`, and `quote_timestamp` when supplied by Kite. The dashboard displays retrieval time in IST. Missing quotes are null/Unavailable, never fabricated. Outside market hours, latest quotes may be from the previous session.

Errors use `{ "error": { "code": "...", "message": "..." } }` for application failures. Missing/expired sessions return 401, invalid callbacks 400, unconfigured credentials 503, and upstream failures 502. Failed refreshes clear displayed values. Native routing errors retain FastAPI's standard response format.

## Security and session lifecycle

Access tokens stay in local server memory only. No credential files, cookies, browser responses, or logs contain access tokens. Restarting loses the session. Tokens are conservatively expired at the next 06:00 IST boundary and cleared immediately when Kite returns a token error. There is no automatic reauthentication. A locally available session can still be revoked upstream; snapshot retrieval detects this.

Structured application logs contain only fixed event names, timestamp, severity and status. Exception text, request query strings, upstream payloads and secrets are excluded. SDK debug logging is disabled. Start with `--no-access-log` so the callback request token is not recorded by Uvicorn. Do not enable HTTP wire logging or add a proxy that logs callback query strings.

Authentication uses a short-lived, single-use random state bound to an HttpOnly SameSite cookie via Kite's `redirect_params`. Responses disable caching and referrer forwarding. The API key necessarily appears in the official login URL; the API secret and access token never do.

## Architecture and verification

`config.py` loads configuration; `session.py` owns session lifetime; `market.py` retains the unchanged REST snapshot implementation. `main.py` wires the stream into FastAPI lifespan and exposes read-only state routes. It contains no WebSocket implementation.

The streaming modules are:

- `streaming/instruments.py`: caches instrument metadata per exchange and IST date, resolves exact exchange/trading-symbol/segment matches, and fails safely if required indices are missing or ambiguous. `find(client, exchange, **criteria)` preserves all metadata columns so future expiry/strike queries can reuse it. No tokens are hardcoded; no derivative subscriptions are implemented.
- `streaming/kite_stream.py`: one locked session owner. A background monitor checks session changes/expiry once per second, discovers instruments, and starts one KiteTicker connection. Re-login detaches the previous connection and resets state. Generation guards discard late callbacks from old sessions. Metadata/startup failures get at most three attempts with backoff; KiteTicker gets five reconnect attempts with exponential delay capped at 30 seconds. Exhaustion remains disconnected until manual re-login or app restart. Quiet ticks do not trigger reconnects. A token rejection clears that session without invalidating a newer login.
- `streaming/transport.py`: marshals connect/disconnect operations onto Twisted's reactor. Pending handshakes and active sockets are cancelled before starting a replacement. The SDK is pinned to the tested version because the adapter retains its factory connector for cancellation. Shutdown stops monitoring/retries, disconnects the socket, and drains queued teardown. The daemon reactor stays idle until process exit (Twisted cannot restart a stopped reactor).
- `streaming/market_state.py`: thread-safe in-memory quotes with null unavailable fields, counts, and a 30-second freshness threshold. It publishes immutable broker-independent `Tick` events into bounded subscriber queues. Future consumers use `state.subscribe()`/`unsubscribe()` and drain their queue independently; overflow drops new events and increments `dropped_events` instead of blocking the market feed. Consumers requiring complete tick histories must handle overflow before analytics are implemented.

Full streaming mode supplies the index fields available from Kite. No equity volume, OI, or depth is inferred. Local receipt times use UTC; naive exchange timestamps emitted by the SDK are interpreted in the host timezone used by its `datetime.fromtimestamp` parser. The dashboard formats dates in IST.

`/api/stream/status` exposes `status` (authentication_required, connecting, connected, reconnecting, disconnected or stale), `websocket_status` (transport state), `last_connected_at`, `last_disconnected_at`, `last_tick_received_at`, and `stale`. `/api/market/live` adds instrument token, trading symbol, friendly name, price, OHLC, exchange timestamp, receipt timestamp, count, and per-instrument stale status. Instrument tokens are public identifiers, not authentication credentials. `market_open` is currently null: freshness is not an exchange calendar. A connected but quiet socket reports `websocket_status: connected`, `status: stale`, not an application failure.

The dashboard prefers received WebSocket values, labels old values STALE / MARKET CLOSED, and uses the unchanged REST snapshot only for instruments without any streaming price. Fallback requests are limited to one per 15 seconds, labelled with retrieval time, and never represented as ticks. SDK/Twisted connection-reason logs are suppressed because upstream errors can embed credential-bearing URLs. Application logs remain fixed structured events.

Tests inject a mock SDK client and make no external API requests. They cover authentication/state replay, expiry, missing credentials, snapshot mapping, partial quotes, sanitized failures, dashboard assets and security headers. Live login and quote entitlement must additionally be tested with your own credentials:

1. Run pytest successfully.
2. Start the app and check `/health`.
3. Configure credentials, log in manually and verify all three quotes and timestamp.
4. Restart the app and verify the dashboard requires login again.

Phase 2 live acceptance: restart the app using the same command, manually authenticate, confirm WebSocket connectivity, and during market activity verify tick timestamps/counts advance at `/api/market/live`. Outside market hours, STALE / MARKET CLOSED with a connected socket is expected. Phase 3 is not implemented.

Tests use only mock credentials and connections. Run `python -m pytest -q` for authentication, REST regression, streaming lifecycle, state, metadata, expiry, concurrency and security checks. Optional dashboard behavior tests use Node's built-in runner: `node --test tests/dashboard.test.cjs` (no npm packages). The installed Starlette version emits one existing TestClient/httpx deprecation warning. Phase 2 real-account WebSocket acceptance remains a manual check; this implementation does not claim it has run. The existing `.venv` can be used directly via `.\.venv\Scripts\python.exe`.

References: [official authentication documentation](https://kite.trade/docs/connect/v3/user/), [quote documentation](https://kite.trade/docs/connect/v3/market-quotes/), [official Python SDK](https://github.com/zerodha/pykiteconnect).
