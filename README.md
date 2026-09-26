# NIFTY Signal Engine

A local dashboard for NIFTY, BANKNIFTY and INDIA VIX using Zerodha Kite Connect. Includes market analysis, signals and manual trade alerts. It does not place broker orders.

## 1. Set up Python (Windows PowerShell)

Install Python 3.11 or newer. Open PowerShell in the project folder, then run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `py` reports that Python is not installed, use `python -m venv .venv` instead, provided `python --version` works. Virtual environment activation is optional with the commands below.

## 2. Configure Zerodha

Create `.env` in the project root only if it does not already exist:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Enter your Kite credentials in that file:

```dotenv
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
KITE_REDIRECT_URL=http://127.0.0.1:8000/kite/callback
```

Set the same redirect URL in your Kite developer console. Your Kite app needs live market-data access. Keep `.env` private; it is excluded from Git. Leave existing credentials in place if already configured.

## 3. Start the app

From the project folder:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), click **Connect to Zerodha**, and complete the login manually. Keep the terminal running. Press **Ctrl+C** to stop.

Run one application process, without extra workers or auto-reload. Restart after changing `.env`; restarting also requires logging in again. Local market history and manual trades remain in the Git-ignored SQLite database.

## Everyday use

- Start the app with the command above and connect to Zerodha.
- Quiet markets or missing fresh data can show STALE or NO TRADE.
- Record a manual trade only after you actually enter it in Kite.
- Use **TEST ALARM** to enable and check browser sound. Keep the dashboard open for alerts. Stop alarms continue until acknowledged or you confirm the trade exit.

## Optional: run tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp
```

With Node.js installed:

```powershell
node --test tests/dashboard.test.cjs tests/structure.test.cjs tests/options.test.cjs tests/signals.test.cjs tests/trades.test.cjs
```

## Single-server cloud deployment

Use one Linux VM (for example EC2 Mumbai), Docker Compose and an HTTPS reverse proxy on the host. No cloud-specific services are required.

```text
Internet
  -> HTTPS reverse proxy (owner-only access)
  -> 127.0.0.1:8000
  -> FastAPI single worker
  -> Kite WebSocket / signals / trade monitor
  -> persistent SQLite /app/data/market.sqlite3
```

**Run exactly one container and one Uvicorn worker.** Sessions and live state are in memory. Do not scale the service or enable reload. The image runs as non-root UID/GID `10001`; Compose allows graceful SIGTERM shutdown before stopping it.

### Configure the server

Install Docker Engine/Compose. In the checkout, create the host `.env` from `.env.example` and set your credentials plus:

```dotenv
KITE_REDIRECT_URL=https://trade.example.com/kite/callback
APP_ALLOWED_HOSTS=trade.example.com,127.0.0.1,localhost
MARKET_DB_PATH=/app/data/market.sqlite3
PROXY_TRUSTED_IPS=127.0.0.1
```

Replace the domain and register the identical HTTPS callback in Kite. Allowed hosts must be exact names without schemes, ports or wildcards. Prepare the bind-mounted directory on Linux:

```bash
mkdir -p data
sudo chown 10001:10001 data
sudo chmod 700 data
chmod 600 .env
docker compose build
docker compose create
docker network inspect nifty-signal-engine_default --format '{{(index .IPAM.Config 0).Gateway}}'
```

For a host reverse proxy on a Linux Docker bridge, set `PROXY_TRUSTED_IPS` in `.env` to the gateway IP printed above (the host's source address seen by the container). For a different network layout, use only the actual proxy peer IP. Do not use `*` or trust public/client networks. Uvicorn accepts forwarded scheme/client headers only from these peers; this is required for HTTPS same-origin POST checks. See [Uvicorn proxy settings](https://www.uvicorn.org/settings/#http).

### Configure HTTPS and owner access

Point the domain at the VM. Allow inbound HTTPS (and HTTP if needed for certificate issuance); keep port 8000 closed in the VM firewall/security group. Compose publishes it only on host loopback. Restrict the proxy to the owner using a VPN/access gateway or HTTPS authentication: **Kite login is not application access control**.

For an Nginx HTTPS virtual host with a valid certificate, use the following location. Create the referenced password file separately; never commit passwords. Keep unknown virtual hosts rejected and preserve the original Host header. Overwrite forwarded headers rather than trusting client-supplied values:

```nginx
location / {
    auth_basic "Private market dashboard";
    auth_basic_user_file /etc/nginx/signal-engine.htpasswd;
    access_log off;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

Keep callback query strings out of proxy/access logs. The application retains its same-origin JSON checks for journal mutations. `.env` is passed at runtime and excluded from the image.

### Start and operate

```bash
docker compose up -d --build
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
docker compose logs --tail=50 signal-engine
```

Open your HTTPS dashboard and authenticate with Zerodha manually each trading day and after application restarts. No automatic login or orders are performed.

- `/health` and `/health/live`: HTTP 200 without Kite login; Compose uses liveness.
- `/health/ready`: HTTP 200 when configured, authenticated, connected, both workers running and SQLite accessible; otherwise HTTP 503 with safe component states. It does not promise current ticks or a qualified signal. Missing Kite login must not trigger a restart loop.
- Host `./data` persists at `/app/data`; the database, WAL and SHM files stay together. Compose overrides `MARKET_DB_PATH` to the mounted path. Invalid/unwritable paths fail startup rather than falling back to temporary storage.
- Back up before updates and on a regular schedule. Stop the service with `docker compose stop`, back up the entire `data` directory to protected storage, then run `docker compose start`. For online backups, use SQLite's backup API; do not copy only the database while writes are active. Test restoring backups.

Use `docker compose down` for a graceful shutdown; it leaves the host data directory intact. The container's writable data mount must remain owned by UID/GID `10001` after restoration. Compose's shutdown allowance is documented in [the service reference](https://docs.docker.com/reference/compose-file/services/#stop_grace_period).
