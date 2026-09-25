import logging
import time
from datetime import datetime, timedelta
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread

from kiteconnect.exceptions import TokenException

from app.analytics.session import local
from app.analytics.storage import CandleStore
from app.options.analytics import OptionsAnalytics, enrich
from app.options.config import OptionsConfig
from app.options.discovery import OptionDiscovery, SubscriptionWindow, atm
from app.options.state import OptionBook, finite_json, nonnegative, normalize_quote
from app.options.storage import OptionsStore

logger = logging.getLogger("market_app")
INDICES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}


class OptionsService:
    def __init__(self, stream, session, client_factory, db_path, config=None, clock=None):
        self.stream, self.session, self.client_factory = stream, session, client_factory
        self.config, self.clock = config or OptionsConfig.load(), clock or stream.state.clock
        self.discovery = OptionDiscovery(stream.resolver)
        self.window = SubscriptionWindow()
        self.book = OptionBook()
        self.database = CandleStore(db_path)
        self.store = OptionsStore(self.database)
        self.lock = RLock()
        self.stop = Event()
        self.threads = []
        self.queue = Queue(maxsize=10000)
        self.snapshots = {}
        self.active_contracts = {}
        self.requested = {}
        self.previous = {}
        self.last_ticks = {}
        self._session_key = None
        self._metadata_at = 0
        self._last_refresh = 0
        self._last_persist = None
        self._generation = 0
        self._baseline_cursor = 0
        self._baseline_retry = {}
        self.dropped_ticks = 0
        self.status = "authentication_required"
        self._closed = False

    def start(self):
        if self.threads or self.stop.is_set():
            return
        self.stream.set_option_consumer(self.enqueue)
        for target in (self._ticks, self._refresh_loop, self._baseline_loop):
            thread = Thread(target=target, daemon=True, name="options-worker")
            self.threads.append(thread)
            thread.start()

    def enqueue(self, token, quote, received):
        try:
            self.queue.put_nowait((self._generation, token, quote, received))
        except Full:
            self.dropped_ticks += 1

    def _ticks(self):
        while not self.stop.is_set():
            try:
                generation, token, quote, received = self.queue.get(timeout=0.5)
                self.process_tick(generation, token, quote, received)
            except Empty:
                pass
            except Exception:
                logger.warning("", extra={"event": "options_tick_failed"})

    def process_tick(self, generation, token, quote, received):
        with self.lock:
            contract = self.active_contracts.get(token)
            if (generation != self._generation or contract is None or not self.session.get()
                    or self._session_key is None or self.session.get() != self._session_key[0]):
                return
            self.book.update(contract, quote, received)
            self.last_ticks[(contract.index, contract.expiry)] = received.isoformat()

    def _check_session(self):
        token = self.session.get()
        key = (token, local(self.clock()).date())
        with self.lock:
            if key != self._session_key:
                self._session_key = key
                self._generation += 1
                self.book.clear()
                self.snapshots.clear()
                self.previous.clear()
                self.last_ticks.clear()
                self._last_refresh = self._metadata_at = 0
                self._baseline_retry.clear()
            self.status = "ready" if token else "authentication_required"
        return token

    def _context(self, index):
        rows = self.stream.state.snapshot()["instruments"]
        spot_row = next((r for r in rows if r["trading_symbol"] == INDICES[index]), None)
        future_row = next((r for r in rows if r.get("alias") == index+"_FUT"), None)
        now = self.clock()
        def price(row):
            received = row.get("last_tick_received_at") if row else None
            return row.get("last_price") if received and 0 <= (now-datetime.fromisoformat(received)).total_seconds() <= self.config.stale_seconds else None
        spot, future = price(spot_row), price(future_row)
        return spot, future

    def cycle(self, force=False):
        token = self._check_session()
        if not token:
            self.stream.set_option_contracts([])
            return
        client = self.client_factory()
        client.set_access_token(token)
        now = local(self.clock())
        try:
            if not self.discovery.ready or time.monotonic()-self._metadata_at >= 900 or self._metadata_at == 0:
                if self.discovery.ready:
                    self.discovery.resolver.invalidate("NFO")
                self.discovery.refresh(client)
                self._metadata_at = time.monotonic()
            selected = []
            targets = []
            for index in INDICES:
                choices = self.discovery.selections(index)
                nearest = choices["nearest"]
                spot, _ = self._context(index)
                if nearest:
                    targets.append((index, nearest))
                    selected += self.window.select(index, nearest, self.discovery.chain(index, nearest), spot)
                with self.lock:
                    requested = self.requested.get(index)
                if requested in self.discovery.get_expiries(index) and requested != nearest:
                    targets.append((index, requested))
            with self.lock:
                self.active_contracts = {c.instrument_token: c for c in selected}
                self.snapshots = {k: v for k, v in self.snapshots.items() if k in targets}
            self.stream.set_option_contracts(selected)
            if force or time.monotonic()-self._last_refresh >= self.config.refresh_seconds:
                for index, expiry in targets:
                    if self.stop.is_set() or self.session.get() != token:
                        return
                    self.refresh_chain(client, index, expiry, token)
                self._last_refresh = time.monotonic()
            now = local(self.clock())  # Snapshot time must follow, not precede, REST I/O.
            minute = now.replace(second=0, microsecond=0)
            if minute != self._last_persist:
                for index in INDICES:
                    summary, rows = self.response(index, window=5)
                    # response returns all discovered contracts, including REST
                    # quotes outside the live window and fresh streaming overlays.
                    # Persist independently of ATM/spot availability.
                    self.store.save_chain(summary, rows, now)
                    center = summary["atm"]
                    strikes = sorted({r["strike"] for r in rows})
                    if center is not None:
                        position = strikes.index(center)
                        selected_strikes = set(strikes[max(0, position-5):position+6])
                        self.store.save(summary, [r for r in rows if r["strike"] in selected_strikes], local(self.clock()))
                self._last_persist = minute
        except TokenException:
            self.session.clear_if(token)
            self.status = "authentication_required"
        except Exception:
            self.status = "temporarily_unavailable"
            logger.warning("", extra={"event": "options_refresh_failed"})

    def refresh_chain(self, client, index, expiry, token):
        contracts = self.discovery.chain(index, expiry)
        future = self.discovery.matching_future(index, expiry)
        symbols = ["NFO:"+c.trading_symbol for c in contracts]
        if future:
            symbols.append("NFO:"+future["tradingsymbol"])
        quotes = {}
        started = self.clock()
        # 200 is safely below Kite's 500-instrument full quote batch limit.
        for offset in range(0, len(symbols), 200):
            if self.stop.is_set() or self.session.get() != token:
                return
            try:
                batch = client.quote(symbols[offset:offset+200])
                received = self.clock()
                for symbol, raw in batch.items():
                    if isinstance(raw, dict):
                        quotes[symbol] = normalize_quote(raw, received, "rest")
            except TokenException:
                raise
            except Exception:
                logger.warning("", extra={"event": "options_quote_batch_failed"})
        if self.session.get() != token:
            return
        with self.lock:
            for contract in contracts:
                quote = quotes.get("NFO:"+contract.trading_symbol)
                if quote is not None:
                    self.book.update(contract, quote, self.clock())
            self.snapshots[(index, expiry)] = {"quotes": quotes, "at": self.clock().isoformat(),
                                                "started_at": started.isoformat(), "future": future}

    def _refresh_loop(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception:
                self.status = "temporarily_unavailable"
                logger.warning("", extra={"event": "options_worker_failed"})
            self.stop.wait(1)

    def baseline_once(self):
        token = self.session.get()
        if not token:
            return
        day = local(self.clock()).date()
        with self.lock:
            targets = list(self.snapshots)
        if targets:
            position = self._baseline_cursor % len(targets)
            targets = targets[position:] + targets[:position]
            self._baseline_cursor += 1
        # One contract per pass keeps this background work from monopolizing history APIs.
        for index, expiry in targets:
            spot, _ = self._context(index)
            contracts = sorted(self.discovery.chain(index, expiry), key=lambda c: abs(c.strike-spot) if spot else c.strike)
            for contract in contracts:
                key = (contract.trading_symbol, day)
                with self.lock:
                    if key in self.previous:
                        continue
                    if time.monotonic() < self._baseline_retry.get(key, 0):
                        continue
                cached = self.store.previous(*key)
                if cached is None:
                    client = self.client_factory()
                    client.set_access_token(token)
                    previous_session = self.database.previous(INDICES[index], day)
                    if not previous_session or not previous_session.get("session_date"):
                        return  # Phase 3's authoritative actual-session cache is warming up.
                    try:
                        rows = client.historical_data(contract.instrument_token, day-timedelta(days=45), day-timedelta(days=1), "day", oi=True)
                    except TokenException:
                        self.session.clear_if(token)
                        return
                    except Exception:
                        self._baseline_retry[key] = time.monotonic()+60
                        logger.warning("", extra={"event": "options_previous_oi_failed"})
                        return
                    valid = [r for r in rows if local(r["date"]).date().isoformat() == previous_session["session_date"]]
                    latest = max(valid, key=lambda r: r["date"]) if valid else None
                    cached = {"oi": nonnegative(latest.get("oi")) if latest else None, "session_date": previous_session["session_date"]}
                    if self.session.get() != token or self.stop.is_set():
                        return
                    self.store.save_previous(*key, cached["oi"], cached["session_date"])
                with self.lock:
                    self.previous[key] = cached
                return

    def _baseline_loop(self):
        while not self.stop.is_set():
            try:
                self.baseline_once()
            except TokenException:
                logger.warning("", extra={"event": "options_previous_oi_authentication_failed"})
            except Exception:
                logger.warning("", extra={"event": "options_previous_oi_failed"})
            self.stop.wait(1)

    def response(self, index, expiry=None, window=10):
        now = local(self.clock())
        explicit = expiry is not None
        expiry = expiry or self.discovery.selections(index)["nearest"]
        if expiry is not None and expiry not in self.discovery.get_expiries(index):
            raise ValueError("Expiry is not currently listed")
        if explicit:
            with self.lock:
                self.requested[index] = expiry
        contracts = self.discovery.chain(index, expiry) if expiry else []
        spot, front_future = self._context(index)
        with self.lock:
            snapshot = self.snapshots.get((index, expiry), {})
            quotes = snapshot.get("quotes", {})
            matched = snapshot.get("future")
            future_quote = quotes.get("NFO:"+matched["tradingsymbol"], {}) if matched else {}
            future = future_quote.get("ltp") if self._fresh(future_quote, now) else None
            authenticated = bool(self.session.get()) and self._session_key is not None and self.session.get() == self._session_key[0]
            rows = []
            for contract in contracts:
                quote = quotes.get("NFO:"+contract.trading_symbol, {})
                previous = self.previous.get((contract.trading_symbol, now.date()), {})
                row = self.book.row(contract, quote, previous.get("oi"))
                row["previous_oi_session_date"] = previous.get("session_date")
                rows.append(enrich(row, spot, future, now, self.config, authenticated and self._fresh(quote, now)))
            summary = OptionsAnalytics.summarize(index, expiry, rows, spot, future, len(contracts), now,
                snapshot.get("at"), self.last_ticks.get((index, expiry)), self.config, window)
            # ATM details may use newer streaming quotes; full-chain metrics never do.
            for kind, key in (("CE", "atm_ce"), ("PE", "atm_pe")):
                contract = self.discovery.get_option_contract(index, expiry, summary["atm"], kind)
                if contract:
                    previous = self.previous.get((contract.trading_symbol, now.date()), {})
                    live = self.book.row(contract, previous_oi=previous.get("oi"))
                    if live.get("source") == "stream" and self._fresh(live, now):
                        summary[key] = enrich(live, spot, future, now, self.config, authenticated)
            for row in rows:
                row["distance_from_atm"] = row["strike"]-summary["atm"] if summary["atm"] is not None else None
            # Chain display/persistence can overlay fresh live window observations,
            # but PCR, walls, coverage and max pain above stay tied to the REST batch.
            displayed = []
            for contract, row in zip(contracts, rows):
                previous = self.previous.get((contract.trading_symbol, now.date()), {})
                live = self.book.row(contract, previous_oi=previous.get("oi"))
                if live.get("source") == "stream" and self._fresh(live, now):
                    live = enrich(live, spot, future, now, self.config, authenticated)
                    live["distance_from_atm"] = row["distance_from_atm"]
                    live["previous_oi_session_date"] = previous.get("session_date")
                    displayed.append(live)
                else:
                    displayed.append(row)
            summary.update(status=self.status, front_month_futures=front_future,
                           futures_basis="selected_expiry_match_only", snapshot_started_at=snapshot.get("started_at"),
                           expiries=[d.isoformat() for d in self.discovery.get_expiries(index)],
                           expiry_selection={k: d.isoformat() if d else None for k,d in self.discovery.selections(index).items()},
                           dropped_stream_ticks=self.dropped_ticks)
            return finite_json(summary), finite_json(displayed)

    def _fresh(self, quote, now):
        if not quote.get("received_at"):
            return False
        received = datetime.fromisoformat(quote["received_at"])
        event = datetime.fromisoformat(quote["timestamp"]) if quote.get("timestamp") else received
        return (0 <= (now-received).total_seconds() <= self.config.stale_seconds
                and -5 <= (now-event).total_seconds() <= self.config.stale_seconds)

    def shutdown(self):
        if self._closed:
            return
        self.stop.set()
        self.stream.set_option_consumer(None)
        for thread in self.threads:
            thread.join(timeout=20)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Options shutdown timed out")
        self.database.close()
        self._closed = True
