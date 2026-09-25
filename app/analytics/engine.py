import logging
from datetime import datetime, time
from queue import Empty
from threading import Event, RLock, Thread

from kiteconnect.exceptions import TokenException

from app.analytics.candles import CandleAggregator
from app.analytics.history import HistoryRecovery
from app.analytics.indicators import VWAP, momentum
from app.analytics.session import IST, local
from app.analytics.storage import CandleStore

logger = logging.getLogger("market_app")
SPOTS = {"nifty": "NIFTY 50", "banknifty": "NIFTY BANK"}


class MarketEngine:
    def __init__(self, state, session, client_factory, db_path, clock=None, feed_status=None):
        self.state, self.session, self.client_factory = state, session, client_factory
        self.clock = clock or state.clock
        self.store = CandleStore(db_path)
        self.aggregator = CandleAggregator(self.store)
        self.stop_event = Event()
        self.recovery = HistoryRecovery(self.store, self.aggregator, lambda: self.stop_event.wait(0.4))
        self.queue = state.subscribe(maxsize=20000)
        self.lock = RLock()
        self.latest = {}
        self.vwaps = {}
        self.threads = []
        self.recovery_status = "waiting_for_session"
        self.dropped_at_start = state.dropped_events
        self.last_dropped = self.dropped_at_start
        self.last_session_token = None
        self.feed_status = feed_status
        self.last_connected = None

    def start(self):
        if self.threads:
            return
        for target in (self._consume, self._recover):
            thread = Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def _consume(self):
        while not self.stop_event.is_set():
            try:
                token = self.session.get()
                if token != self.last_session_token:
                    self.aggregator.mark_gap()
                    with self.lock:
                        self.latest.clear()
                        self.vwaps.clear()
                    self.last_session_token = token
                if self.feed_status is not None:
                    status = self.feed_status()
                    connected = status.get("websocket_status") == "connected"
                    if self.last_connected is True and not connected:
                        self.aggregator.mark_gap()
                    self.last_connected = connected
                tick = self.queue.get(timeout=0.5)
                self.process(tick)
            except Empty:
                pass
            except Exception:
                logger.warning("", extra={"event": "candle_update_failed"})
            try:
                self.aggregator.advance(self.clock())
            except Exception:
                logger.warning("", extra={"event": "candle_persistence_failed"})

    def process(self, tick):
        # Never process stale events from a detached contract/session.
        if not self.session.get():
            return
        if not any(row["instrument_token"] == tick.instrument_token and row["trading_symbol"] == tick.symbol
                   for row in self.state.snapshot()["instruments"]):
            return
        if self.state.dropped_events != self.last_dropped:
            self.aggregator.mark_gap()
            self.last_dropped = self.state.dropped_events
        if not self.aggregator.consume(tick):
            return
        with self.lock:
            previous = self.latest.get(tick.symbol)
            if previous is None or tick.timestamp >= previous.timestamp:
                self.latest[tick.symbol] = tick
                if tick.symbol not in (*SPOTS.values(), "INDIA VIX"):
                    self.vwaps.setdefault(tick.symbol, VWAP()).update(tick)

    def recover_once(self):
        token = self.session.get()
        if not token:
            self.recovery_status = "waiting_for_session"
            return
        instruments = self.state.snapshot()["instruments"]
        if not instruments:
            return
        self.recovery_status = "recovering"
        client = self.client_factory()
        client.set_access_token(token)
        try:
            for instrument in instruments:
                if self.stop_event.is_set() or self.session.get() != token:
                    return
                self.recovery.recover(client, instrument, self.clock())
            self.recovery_status = "ready"
        except TokenException:
            self.session.clear_if(token)
            self.recovery_status = "authentication_required"
        except Exception:
            self.recovery_status = "history_unavailable"
            logger.warning("", extra={"event": "history_recovery_failed"})

    def _recover(self):
        while not self.stop_event.is_set():
            try:
                self.recover_once()
            except Exception:
                self.recovery_status = "history_unavailable"
                logger.warning("", extra={"event": "history_recovery_failed"})
            self.stop_event.wait(60 if self.recovery_status in ("ready", "history_unavailable") else 1)

    def resolve_symbol(self, symbol):
        aliases = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
        rows = self.state.snapshot()["instruments"]
        aliases.update({r["alias"]: r["trading_symbol"] for r in rows if "alias" in r})
        symbol = aliases.get(symbol, symbol)
        allowed = {*SPOTS.values(), "INDIA VIX", *(r["trading_symbol"] for r in rows)}
        return symbol if symbol in allowed else None

    def structure(self):
        now = local(self.clock())
        data = {"as_of": now.isoformat(), "recovery_status": self.recovery_status,
                "session_date": now.date().isoformat(), "dropped_tick_events": self.state.dropped_events - self.dropped_at_start}
        rows = self.state.snapshot()["instruments"]
        with self.lock:
            for name, symbol in SPOTS.items():
                alias = name.upper() + "_FUT"
                future_symbol = next((r["trading_symbol"] for r in rows if r.get("alias") == alias), None)
                spot_tick, fut_tick = self.latest.get(symbol), self.latest.get(future_symbol)
                spot_tick = spot_tick if spot_tick and local(spot_tick.timestamp).date() == now.date() else None
                fut_tick = fut_tick if fut_tick and local(fut_tick.timestamp).date() == now.date() else None
                spot, future = (spot_tick.last_price if spot_tick else None), (fut_tick.last_price if fut_tick else None)
                day = dict(spot_tick.ohlc or ()) if spot_tick else {}
                minutes = self.store.load(symbol, "1m", 1000, now.date())
                cutoff = min(now.replace(second=0, microsecond=0), datetime.combine(now.date(), time(15, 30), IST))
                complete_day_context = (minutes and minutes[0]["start_time"][11:16] == "09:15"
                                        and datetime.fromisoformat(minutes[-1]["end_time"]) >= cutoff
                                        and all(not c["partial"] for c in minutes)
                                        and all(a["end_time"] == b["start_time"] for a, b in zip(minutes, minutes[1:])))
                if complete_day_context:
                    opening = [c for c in minutes if c["start_time"][11:16] == "09:15"]
                    day.setdefault("open", opening[0]["open"] if opening else None)
                    day.setdefault("high", max(c["high"] for c in minutes))
                    day.setdefault("low", min(c["low"] for c in minutes))
                opening = [c for c in minutes if "09:15" <= c["start_time"][11:16] < "09:30" and not c["partial"]]
                or_high = max(c["high"] for c in opening) if len(opening) == 15 else None
                or_low = min(c["low"] for c in opening) if len(opening) == 15 else None
                previous = self.store.previous(symbol, now.date()) or {}
                vwap = self.vwaps.get(future_symbol)
                vw = vwap.snapshot(future) if vwap and vwap.day == now.date() else VWAP().snapshot(future)
                data[name] = {"spot": spot, "future": future, "future_symbol": future_symbol,
                    "futures_basis": future - spot if future is not None and spot is not None else None,
                    "day_open": day.get("open"), "day_high": day.get("high"), "day_low": day.get("low"),
                    "previous_session_date": previous.get("session_date"),
                    "previous_day_high": previous.get("high"), "previous_day_low": previous.get("low"), "previous_day_close": previous.get("close"),
                    "opening_range_high": or_high, "opening_range_low": or_low,
                    "opening_range_state": None if or_high is None or spot is None else "above" if spot > or_high else "below" if spot < or_low else "inside",
                    "futures_vwap": vw["vwap"], "price_vs_vwap": vw["price_vs_vwap"],
                    "vwap_method": vw["vwap_method"], "vwap_full_session": vw["full_session"],
                    "stale": not self.session.get() or not spot_tick or not fut_tick or any((now - local(t.timestamp)).total_seconds() >= 30 for t in (spot_tick, fut_tick) if t)}
                for interval in ("5m", "15m", "30m"):
                    data[name][interval] = momentum(self.aggregator.candles(symbol, interval, 1000))
                data[alias] = vw
        return data

    def shutdown(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=15)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Analytics shutdown timed out")
        self.state.unsubscribe(self.queue)
        self.aggregator.advance(self.clock())
        self.store.close()
