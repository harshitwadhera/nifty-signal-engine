import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from threading import Lock
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request, Query
from typing import Literal
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import ROOT, Settings
from app.logging_config import configure_logging
from app.market import RestMarketDataProvider
from app.session import SessionStore
from app.streaming.kite_stream import KiteStream
from starlette.concurrency import run_in_threadpool
from app.analytics.engine import MarketEngine

logger = logging.getLogger("market_app")


class AppError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def create_app(settings=None, client_factory=None, session=None, provider=None, stream=None, engine=None):
    configure_logging()
    settings = settings or Settings.load()
    session = session or SessionStore()
    provider = provider or RestMarketDataProvider()
    client_factory = client_factory or (lambda: KiteConnect(api_key=settings.api_key, timeout=10, debug=False))
    if stream is None:
        stream = KiteStream(settings, session, client_factory)
        stream.include_futures = True

    @asynccontextmanager
    async def lifespan(app):
        nonlocal engine
        engine = engine or MarketEngine(stream.state, session, client_factory,
                                        os.getenv("MARKET_DB_PATH", str(ROOT / "data" / "market.sqlite3")),
                                        feed_status=stream.status)
        engine.start()
        try:
            stream.start()
            yield
        finally:
            try:
                await run_in_threadpool(stream.shutdown)
            finally:
                await run_in_threadpool(engine.shutdown)

    app = FastAPI(title="Read-only index dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    pending = {}
    pending_lock = Lock()

    @app.middleware("http")
    async def safe_responses(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            logger.error("", extra={"event": "unexpected_error", "status": 500})
            response = JSONResponse({"error": {"code": "internal_error", "message": "An internal error occurred."}}, status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        logger.info("", extra={"event": "request_completed", "status": response.status_code})
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    def require_config():
        if not settings.configured:
            raise AppError(503, "not_configured", "Set Kite credentials in the local .env file.")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/connection")
    def connection():
        return {"configured": settings.configured,
                "connection_status": "session_available" if session.get() else "disconnected"}

    @app.get("/api/market/live")
    def live():
        return stream.live()

    @app.get("/api/stream/status")
    def stream_status():
        return stream.status()

    @app.get("/api/market/structure")
    def structure():
        return engine.structure()

    @app.get("/api/candles/{symbol}")
    def candles(symbol: str, interval: Literal["1m", "5m", "15m", "30m"] = "5m",
                limit: int = Query(default=100, ge=1, le=1000)):
        resolved = engine.resolve_symbol(symbol)
        if resolved is None:
            raise AppError(404, "unknown_symbol", "Symbol is not currently available.")
        return {"symbol": resolved, "interval": interval,
                "candles": engine.aggregator.candles(resolved, interval, limit)}

    @app.get("/kite/login")
    def login():
        require_config()
        state = secrets.token_urlsafe(32)
        with pending_lock:
            now = time.monotonic()
            for key in list(pending):
                if pending[key] <= now:
                    del pending[key]
            if len(pending) >= 100:
                raise AppError(429, "too_many_logins", "Please wait before trying login again.")
            pending[state] = now + 600
        url = client_factory().login_url()
        url += ("&" if "?" in url else "?") + urlencode({"redirect_params": urlencode({"state": state})})
        response = RedirectResponse(url, status_code=303)
        response.set_cookie("kite_login_state", state, httponly=True, samesite="lax", max_age=600,
                            secure=urlparse(settings.redirect_url).scheme == "https", path="/kite")
        return response

    @app.get("/kite/callback")
    def callback(request: Request):
        require_config()
        state = request.query_params.get("state", "")
        cookie = request.cookies.get("kite_login_state", "")
        with pending_lock:
            expiry = pending.pop(state, 0) if state and secrets.compare_digest(state, cookie) else 0
        if expiry <= time.monotonic():
            raise AppError(400, "invalid_login_state", "Login expired or invalid. Start again from the dashboard.")
        request_token = request.query_params.get("request_token", "")
        if not request_token or request.query_params.get("status") not in (None, "success"):
            raise AppError(400, "login_cancelled", "Login was cancelled or no request token was supplied.")
        try:
            data = client_factory().generate_session(request_token, api_secret=settings.api_secret)
            token = data.get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError()
            session.save(token)
        except Exception:
            logger.warning("", extra={"event": "authentication_failed", "status": 502})
            raise AppError(502, "authentication_failed", "Kite authentication failed. Start login again.") from None
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("kite_login_state", path="/kite")
        return response

    @app.get("/api/market/snapshot")
    def snapshot():
        require_config()
        token = session.get()
        if not token:
            raise AppError(401, "login_required", "Connect to Zerodha to retrieve market data.")
        try:
            client = client_factory()
            client.set_access_token(token)
            return provider.snapshot(client)
        except TokenException:
            session.clear()
            raise AppError(401, "session_expired", "Your Kite session expired. Please connect again.") from None
        except Exception:
            logger.warning("", extra={"event": "market_data_failed", "status": 502})
            raise AppError(502, "market_data_unavailable", "Kite market data is unavailable. Please retry shortly.") from None

    app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(ROOT / "app" / "static" / "index.html")

    return app


app = create_app()
