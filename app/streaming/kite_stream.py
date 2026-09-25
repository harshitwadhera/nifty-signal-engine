import logging
import re
import time
from datetime import datetime
from math import isfinite
from threading import Event, RLock, Thread

from kiteconnect.exceptions import TokenException

from app.streaming.instruments import InstrumentResolver
from app.streaming.market_state import MarketState, Tick
from app.streaming.transport import TickerTransport

logger = logging.getLogger("market_app")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


class KiteStream:
    """One session owner; SDK callbacks are generation-checked and never expose errors."""

    def __init__(self, settings, session, client_factory, resolver=None, state=None,
                 transport_factory=None):
        self.settings, self.session, self.client_factory = settings, session, client_factory
        self.resolver = resolver or InstrumentResolver()
        self.state = state or MarketState()
        self.transport_factory = transport_factory or TickerTransport
        self._lock = RLock()
        self._stop = Event()
        self._thread = None
        self._transport = None
        self._token = None
        self._generation = 0
        self._attempts = 0
        self._next_attempt = 0
        self._status = "authentication_required"
        self._last_connected = self._last_disconnected = None
        self._symbols = {}
        self.include_futures = False  # Enabled by production application wiring.
        self._resolution_period = None
        self._option_symbols = {}
        self._option_sink = None

    def set_option_consumer(self, consumer):
        with self._lock:
            self._option_sink = consumer

    def set_option_contracts(self, contracts):
        desired = {c.instrument_token: c.trading_symbol for c in contracts}
        with self._lock:
            if desired == self._option_symbols:
                return
            removed = sorted(set(self._option_symbols)-set(desired))
            added = sorted(set(desired)-set(self._option_symbols))
            self._option_symbols = desired
            if self._transport and self._status == "connected":
                self._transport.update_subscriptions(added, removed)

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = Thread(target=self._run, daemon=True, name="market-feed-owner")
                self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                logger.warning("", extra={"event": "feed_monitor_failed"})
            self._stop.wait(1)

    def _detach(self):
        self._generation += 1
        if self._transport:
            self._transport.close()
            self._transport = None
            self._last_disconnected = self.state.clock().isoformat()

    def reconcile(self):
        # This method is also the deterministic test seam; metadata I/O is serialized.
        with self._lock:
            if self._stop.is_set():
                return
            token = self.session.get()
            now = self.resolver.clock()
            period = (now.date(), (now.hour, now.minute) >= (15, 30))
            rollover = self.include_futures and self._resolution_period != period
            if token != self._token or rollover:
                self._detach()
                self.state.reset()
                self._token, self._attempts, self._next_attempt = token, 0, 0
                self._resolution_period = period
            if not token:
                self._status = "authentication_required"
                return
            if self._transport or self._attempts >= 3 or time.monotonic() < self._next_attempt:
                return
            self._attempts += 1
            self._status = "connecting"
            try:
                client = self.client_factory()
                client.set_access_token(token)
                instruments = self.resolver.universe(client) if self.include_futures else self.resolver.indices(client)
                if self.session.get() != token or self._stop.is_set():
                    return
                self.state.reset(instruments)
                self._symbols = {i["instrument_token"]: i["trading_symbol"] for i in instruments}
                transport = self.transport_factory(self.settings.api_key, token)
                self._transport = transport
                generation = self._generation
                ticker = transport.ticker
                ticker.on_connect = lambda ws, response: self._connected(generation, ws)
                ticker.on_ticks = lambda ws, ticks: self._ticks(generation, ticks)
                ticker.on_close = lambda ws, code, reason: self._closed(generation, code, reason)
                ticker.on_error = lambda ws, code, reason: self._closed(generation, code, reason)
                ticker.on_reconnect = lambda ws, attempts: self._transition(generation, "reconnecting")
                ticker.on_noreconnect = lambda ws: self._transition(generation, "disconnected")
                transport.start()
            except TokenException:
                self._invalidate()
            except Exception:
                self._detach()
                self._status = "disconnected"
                self._next_attempt = time.monotonic() + min(30, 2 ** self._attempts)
                logger.warning("", extra={"event": "feed_start_failed"})

    def _valid(self, generation):
        if generation != self._generation or self._stop.is_set():
            return False
        if self.session.get() != self._token or self._token is None:
            self._detach()
            self._status = "authentication_required"
            return False
        return True

    def _invalidate(self):
        # Don't clear a newer login when an older connection fails.
        self.session.clear_if(self._token)
        self._detach()
        self._token = None
        self.state.reset()
        self._status = "authentication_required"

    def _connected(self, generation, ws):
        with self._lock:
            if not self._valid(generation):
                return
            try:
                tokens = list(self._symbols) + list(self._option_symbols)
                existing = getattr(ws, "subscribed_tokens", {})
                obsolete = sorted(set(existing)-set(tokens)) if isinstance(existing, dict) else []
                if obsolete:
                    ws.unsubscribe(obsolete)
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
                self._status = "connected"
                self._last_connected = self.state.clock().isoformat()
            except Exception:
                self._detach()
                self._attempts = 3
                self._status = "disconnected"

    def _transition(self, generation, status):
        with self._lock:
            if self._valid(generation):
                self._status = status
                if status == "disconnected":
                    self._last_disconnected = self.state.clock().isoformat()

    def _closed(self, generation, code, reason):
        with self._lock:
            if not self._valid(generation):
                return
            # Handshake failures may arrive as code 1006 with HTTP status in reason.
            # Inspect privately; never log or serialize this upstream text.
            if code in (401, 403) or re.search(r"\b(401|403|TokenException|TokenError)\b|token.*(invalid|expired)|(invalid|expired).*token", str(reason), re.IGNORECASE):
                self._invalidate()
            else:
                self._status = "disconnected"
                self._last_disconnected = self.state.clock().isoformat()

    def _ticks(self, generation, ticks):
        with self._lock:
            if not self._valid(generation):
                return
            for raw in ticks:
                if not isinstance(raw, dict):
                    continue
                token, price = raw.get("instrument_token"), raw.get("last_price")
                if isinstance(token, int) and token in self._option_symbols:
                    if self._option_sink:
                        try:
                            from app.options.state import normalize_quote
                            received = self.state.clock()
                            self._option_sink(token, normalize_quote(raw, received, "stream"), received)
                        except Exception:
                            logger.warning("", extra={"event": "option_tick_rejected"})
                    continue
                if not isinstance(token, int) or token not in self._symbols or not number(price):
                    continue
                timestamp = raw.get("exchange_timestamp")
                if not isinstance(timestamp, datetime):
                    timestamp = None
                elif timestamp.tzinfo is None:
                    # SDK uses datetime.fromtimestamp(): naive timestamp is host-local.
                    timestamp = timestamp.astimezone()
                raw_ohlc = raw.get("ohlc")
                ohlc = tuple((k, v) for k, v in raw_ohlc.items() if k in {"open", "high", "low", "close"} and number(v)) if isinstance(raw_ohlc, dict) else None
                def quantity(key):
                    value = raw.get(key)
                    return int(value) if number(value) and value >= 0 and int(value) == value else None
                average = raw.get("average_traded_price")
                self.state.update(Tick(token, self._symbols[token], price, self.state.clock(), timestamp, ohlc,
                                       quantity("volume_traded"), quantity("oi"),
                                       average if number(average) and average > 0 else None))

    def status(self):
        with self._lock:
            valid = self.session.get() is not None and self.session.get() == self._token
            connection = self._status if valid else "authentication_required"
            data = self.state.snapshot(connected=connection == "connected")
            status = "stale" if connection == "connected" and data["stale"] else connection
            return {"status": status, "websocket_status": connection,
                    "last_connected_at": self._last_connected,
                    "last_disconnected_at": self._last_disconnected,
                    "last_tick_received_at": data["last_tick_received_at"], "stale": data["stale"],
                    "market_open": None}

    def live(self):
        with self._lock:
            status = self.status()
            return {**status, **self.state.snapshot(connected=status["websocket_status"] == "connected")}

    def shutdown(self):
        self._stop.set()
        with self._lock:
            self._detach()
            self._status = "disconnected"
        if self._thread:
            self._thread.join(timeout=15)
        if self.transport_factory is TickerTransport:
            TickerTransport.flush()
