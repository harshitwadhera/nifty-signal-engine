from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Queue, Full
from threading import RLock


def utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Tick:
    """Broker-independent event. OHLC is immutable (field, value) pairs."""
    instrument_token: int
    trading_symbol: str
    last_price: float
    received_at: datetime
    exchange_timestamp: datetime | None = None
    ohlc: tuple[tuple[str, float], ...] | None = None
    volume: int | None = None  # Cumulative traded volume for the session.
    open_interest: int | None = None
    average_traded_price: float | None = None

    @property
    def symbol(self):
        return self.trading_symbol

    @property
    def timestamp(self):
        return self.exchange_timestamp or self.received_at


class MarketState:
    def __init__(self, clock=utc_now, stale_seconds=30):
        self.clock = clock
        self.stale_seconds = stale_seconds
        self._lock = RLock()
        self._rows = {}
        self._consumers = []
        self.dropped_events = 0

    def subscribe(self, maxsize=1000):
        """Consumers drain their own bounded queue, never blocking the feed thread."""
        if maxsize < 1:
            raise ValueError("Queue must be bounded")
        queue = Queue(maxsize=maxsize)
        with self._lock:
            self._consumers.append(queue)
        return queue

    def unsubscribe(self, queue):
        with self._lock:
            self._consumers.remove(queue)

    def reset(self, instruments=()):
        with self._lock:
            self._rows = {i["instrument_token"]: {**i, "symbol": i["trading_symbol"],
                          "last_price": None, "ohlc": None, "last_exchange_timestamp": None,
                          "last_tick_received_at": None, "tick_count": 0} for i in instruments}

    def update(self, tick: Tick):
        with self._lock:
            row = self._rows.get(tick.instrument_token)
            if row is None or row["trading_symbol"] != tick.trading_symbol:
                return
            row.update(last_price=tick.last_price, last_tick_received_at=tick.received_at,
                       tick_count=row["tick_count"] + 1)
            if tick.ohlc is not None:
                row["ohlc"] = dict(tick.ohlc)
            if tick.exchange_timestamp is not None:
                row["last_exchange_timestamp"] = tick.exchange_timestamp
            for queue in self._consumers:
                try:
                    queue.put_nowait(tick)
                except Full:
                    self.dropped_events += 1

    def snapshot(self, connected=False):
        with self._lock:
            now = self.clock()
            rows = deepcopy(list(self._rows.values()))
        last = None
        for row in rows:
            received = row["last_tick_received_at"]
            row["stale"] = not connected or received is None or (now - received).total_seconds() >= self.stale_seconds
            if received and (last is None or received > last):
                last = received
            for key in ("last_tick_received_at", "last_exchange_timestamp"):
                row[key] = row[key].isoformat() if row[key] else None
        return {"instruments": rows, "last_tick_received_at": last.isoformat() if last else None,
                "stale": not rows or any(row["stale"] for row in rows)}
