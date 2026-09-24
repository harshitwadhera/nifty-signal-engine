# NIFTY market dashboard — Phase 1

Local, single-user, read-only FastAPI application using the official `kiteconnect` Python SDK. Displays NIFTY 50 (`NSE:NIFTY 50`), BANK NIFTY (`NSE:NIFTY BANK`) and INDIA VIX (`NSE:INDIA VIX`). No signals, order placement, automated login, or WebSocket implementation.

## Windows setup (PowerShell)

Install Python 3.11 or newer, including the Windows Python launcher, then open PowerShell in this project:

```powershell
cd "C:\Users\harsh\OneDrive\Documents\ChatGPT\nifty-signal-engine"
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
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

Run one worker. Do not enable auto-reload if you want to retain the in-memory session. This Phase 1 app is intended for loopback use only, not public hosting or multiple users. If this directory is synced by OneDrive, exclude `.env` from syncing or use a nonsynced local project directory for real credentials.

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET /` | Dashboard, polls every 15 seconds |
| `GET /health` | Process liveness; does not check Kite |
| `GET /kite/login` | Redirect to manual Zerodha login |
| `GET /kite/callback` | Validate login state and exchange request token |
| `GET /api/connection` | Configuration and local session availability; no remote validation |
| `GET /api/market/snapshot` | Fetch three index quotes; successful retrieval confirms connection |

Snapshots contain `connection_status`, `last_updated` (UTC retrieval timestamp), `partial`, and an `instruments` array with `symbol`, `name`, `value`, `available`, and `quote_timestamp` when supplied by Kite. The dashboard displays retrieval time in IST. Missing quotes are null/Unavailable, never fabricated. Outside market hours, latest quotes may be from the previous session.

Errors use `{ "error": { "code": "...", "message": "..." } }` for application failures. Missing/expired sessions return 401, invalid callbacks 400, unconfigured credentials 503, and upstream failures 502. Failed refreshes clear displayed values. Native routing errors retain FastAPI's standard response format.

## Security and session lifecycle

Access tokens stay in local server memory only. No credential files, cookies, browser responses, or logs contain access tokens. Restarting loses the session. Tokens are conservatively expired at the next 06:00 IST boundary and cleared immediately when Kite returns a token error. There is no automatic reauthentication. A locally available session can still be revoked upstream; snapshot retrieval detects this.

Structured application logs contain only fixed event names, timestamp, severity and status. Exception text, request query strings, upstream payloads and secrets are excluded. SDK debug logging is disabled. Start with `--no-access-log` so the callback request token is not recorded by Uvicorn. Do not enable HTTP wire logging or add a proxy that logs callback query strings.

Authentication uses a short-lived, single-use random state bound to an HttpOnly SameSite cookie via Kite's `redirect_params`. Responses disable caching and referrer forwarding. The API key necessarily appears in the official login URL; the API secret and access token never do.

## Architecture and verification

`config.py` loads environment configuration; `session.py` owns session lifetime; `market.py` defines a `MarketDataProvider` interface and REST implementation; `main.py` handles HTTP/authentication and dependency wiring. Synchronous SDK calls run in FastAPI worker threads with a 10-second timeout. A future WebSocket cache can implement the provider contract without coupling the dashboard to a transport. Phase 2 is intentionally not implemented.

Tests inject a mock SDK client and make no external API requests. They cover authentication/state replay, expiry, missing credentials, snapshot mapping, partial quotes, sanitized failures, dashboard assets and security headers. Live login and quote entitlement must additionally be tested with your own credentials:

1. Run pytest successfully.
2. Start the app and check `/health`.
3. Configure credentials, log in manually and verify all three quotes and timestamp.
4. Restart the app and verify the dashboard requires login again.

Do not proceed to Phase 2 until this live Phase 1 check succeeds.

Initial local verification: **18 pytest tests passed** on Python 3.12. The installed Starlette version emits one TestClient/httpx deprecation warning; it does not affect the passing tests. Live Zerodha authentication and quotes have not been tested without user credentials. A local `.venv` has been created using Codex's bundled Python because the Windows launcher found no installed Python; use `.\.venv\Scripts\python.exe` directly to run it. For a fresh standalone setup, install Python and follow the commands above.

References: [official authentication documentation](https://kite.trade/docs/connect/v3/user/), [quote documentation](https://kite.trade/docs/connect/v3/market-quotes/), [official Python SDK](https://github.com/zerodha/pykiteconnect).
