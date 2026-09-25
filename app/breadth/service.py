import logging
import time
from datetime import datetime
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread

from kiteconnect.exceptions import TokenException

from app.analytics.candles import CandleAggregator
from app.analytics.indicators import ema
from app.analytics.session import local
from app.analytics.storage import CandleStore
from app.breadth.metrics import calculate
from app.breadth.universe import ConstituentSource, resolve_constituents

logger = logging.getLogger("market_app")


class BreadthService:
    """Bounded normalized-tick consumer on the existing connection.

    EMA warm-up uses complete live candles only; no extra history request load.
    State resets on authentication/day changes, and partial bars never seed EMA.
    """
    def __init__(self, stream, session, client_factory, source=None):
        self.stream, self.session, self.client_factory = stream, session, client_factory
        self.source = source or ConstituentSource()
        self.clock = stream.state.clock
        self.lock = RLock()
        self.stop = Event()
        self.queue = Queue(maxsize=20000)
        self.thread = None
        self.key = None
        self.universe, self.contracts, self.latest = {}, {}, {}
        self.db = CandleStore(":memory:")
        self.candles = CandleAggregator(self.db)
        self.retry_at = 0
        self.loaded = False
        self.connected = False
        self.dropped = 0
        self.closed = False

    def start(self):
        if self.thread or self.stop.is_set():
            return
        self.stream.set_breadth_consumer(self.enqueue)
        self.thread = Thread(target=self._run, daemon=True, name="breadth-worker")
        self.thread.start()

    def enqueue(self, tick):
        try:
            self.queue.put_nowait((self.key, tick))
        except Full:
            self.dropped += 1

    def reconcile(self):
        token = self.session.get()
        day = local(self.clock()).date()
        key = (token, day)
        if key != self.key:
            self.stream.set_breadth_contracts([])
            with self.lock:
                self.key = key
                self.latest.clear()
                self.contracts.clear()
                self.universe.clear()
                self.db.close()
                self.db = CandleStore(":memory:")
                self.candles = CandleAggregator(self.db)
                self.loaded = False
                self.retry_at = 0
        connected = self.stream.status()["websocket_status"] == "connected"
        if not connected:
            self.candles.mark_gap()
        self.connected = connected
        if not token or self.loaded or time.monotonic() < self.retry_at:
            return
        self.retry_at = time.monotonic()+300
        universe = self.source.load(day)
        client = self.client_factory()
        client.set_access_token(token)
        contracts = resolve_constituents(self.stream.resolver, client, universe)
        if self.session.get() != token or self.stop.is_set():
            return
        with self.lock:
            self.universe = universe
            self.contracts = {r["instrument_token"]: r["trading_symbol"] for r in contracts}
            expected = {s for item in universe.values() for s in item["symbols"]}
            self.loaded = all(item["membership_valid"] for item in universe.values()) and set(self.contracts.values()) == expected
        self.stream.set_breadth_contracts(contracts)

    def process(self, key, tick):
        with self.lock:
            if key != self.key or not self.key or self.key[0] != self.session.get() or not self.session.get():
                return
            if self.contracts.get(tick.instrument_token) != tick.symbol or local(tick.timestamp).date() != self.key[1]:
                return
            previous = self.latest.get(tick.symbol)
            if previous and tick.timestamp < previous.timestamp:
                return
            if self.candles.consume(tick):
                self.latest[tick.symbol] = tick

    def _run(self):
        dropped = 0
        while not self.stop.is_set():
            try:
                self.reconcile()
                if self.dropped != dropped:
                    self.candles.mark_gap()
                    dropped = self.dropped
                for _ in range(500):
                    try:
                        self.process(*self.queue.get_nowait())
                    except Empty:
                        break
                with self.lock:
                    self.candles.advance(self.clock())
            except TokenException:
                if self.key:
                    self.session.clear_if(self.key[0])
            except Exception:
                logger.warning("", extra={"event": "breadth_worker_unavailable"})
            self.stop.wait(.2)

    def snapshot(self, index):
        now = self.clock()
        connected = self.stream.status()["websocket_status"] == "connected"
        with self.lock:
            valid_session = bool(self.key and self.key == (self.session.get(), local(now).date()) and self.session.get())
            observations = {}
            for symbol, tick in self.latest.items():
                row = {"last_price": tick.last_price, "previous_close": dict(tick.ohlc or ()).get("close"),
                       "stale": not connected or not valid_session or not 0 <= (now-tick.timestamp).total_seconds() < 30
                       or not 0 <= (now-tick.received_at).total_seconds() < 30}
                for interval in ("5m", "15m"):
                    bars = self.candles.candles(symbol, interval, 100)
                    # Use a contiguous suffix only. A gap/partial bar restarts warm-up.
                    closes, end = [], None
                    for bar in reversed(bars):
                        if not bar["completed"]:
                            continue
                        if bar["partial"] or (end is not None and bar["end_time"] != end):
                            break
                        if end is None and (now-datetime.fromisoformat(bar["end_time"])).total_seconds() > int(interval[:-1])*60+5:
                            break
                        closes.append(bar["close"])
                        end = bar["start_time"]
                    row[interval+"_ema20"] = ema(list(reversed(closes)), 20)
                observations[symbol] = row
            membership = self.universe.get(index, {"symbols": [], "membership_valid": False})
            result = calculate(index, membership, observations, now.isoformat())
            result["dropped_ticks"] = self.dropped
            return result

    def shutdown(self):
        if self.closed:
            return
        self.stop.set()
        self.stream.set_breadth_consumer(None)
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError("Breadth shutdown timed out")
        self.stream.set_breadth_contracts([])
        self.db.close()
        self.closed = True
