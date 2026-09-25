from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, time
from math import isfinite
from threading import RLock

from app.analytics.session import local


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def nonnegative(value):
    return value if numeric(value) and value >= 0 else None


def finite_json(value):
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(item) for item in value]
    return value


def timestamp(value):
    if not isinstance(value, datetime):
        return None
    return value.astimezone() if value.tzinfo is None else value


def normalize_quote(raw, received, source):
    depth = raw.get("depth") or {}
    levels = {}
    for side in ("buy", "sell"):
        levels[side] = [{"price": nonnegative(p.get("price")), "quantity": nonnegative(p.get("quantity")),
                         "orders": nonnegative(p.get("orders"))} for p in depth.get(side, [])[:5] if isinstance(p, dict)]
    bid = levels["buy"][0] if levels["buy"] else {}
    ask = levels["sell"][0] if levels["sell"] else {}
    exchange = timestamp(raw.get("exchange_timestamp") or raw.get("timestamp"))
    return {"ltp": nonnegative(raw.get("last_price")), "oi": nonnegative(raw.get("oi")),
            "volume": nonnegative(raw.get("volume_traded") if source == "stream" else raw.get("volume")),
            "bid": bid.get("price"), "ask": ask.get("price"),
            "bid_quantity": bid.get("quantity"), "ask_quantity": ask.get("quantity"), "depth": levels,
            "timestamp": exchange.isoformat() if exchange else None,
            "received_at": received.isoformat(), "source": source}


@dataclass
class OptionState:
    market: dict = field(default_factory=dict)
    baseline_oi: float | None = None
    baseline_price: float | None = None
    baseline_volume: float | None = None
    baseline_at: str | None = None
    baseline_events: dict = field(default_factory=dict)


class OptionBook:
    def __init__(self):
        self.lock = RLock()
        self.states = {}

    def clear(self):
        with self.lock:
            self.states.clear()

    def update(self, contract, quote, now):
        with self.lock:
            state = self.states.setdefault(contract.trading_symbol, OptionState())
            old = state.market
            event = datetime.fromisoformat(quote["timestamp"] or quote["received_at"])
            old_event = datetime.fromisoformat(old["timestamp"] or old["received_at"]) if old else None
            if old_event and event < old_event:
                return
            state.market = deepcopy(quote)
            local_event, current = local(event), local(now)
            eligible = (local_event.date() == current.date() and current.weekday() < 5
                        and time(9, 15) <= local_event.time() < time(15, 30)
                        and (event-now).total_seconds() <= 5)
            if eligible:
                for baseline, name in (("baseline_oi", "oi"), ("baseline_price", "ltp"), ("baseline_volume", "volume")):
                    if getattr(state, baseline) is None and quote[name] is not None:
                        setattr(state, baseline, quote[name])
                        state.baseline_events[name] = event
                if state.baseline_oi is not None and state.baseline_at is None:
                    state.baseline_at = quote["received_at"]

    def row(self, contract, quote=None, previous_oi=None):
        with self.lock:
            state = self.states.get(contract.trading_symbol, OptionState())
            data = deepcopy(quote if quote is not None else state.market)
            data = {**{k: None for k in ("ltp", "oi", "volume", "bid", "ask", "bid_quantity", "ask_quantity", "timestamp", "received_at", "source")}, **data}
            oi, price, volume = data["oi"], data["ltp"], data["volume"]
            event = datetime.fromisoformat(data["timestamp"] or data["received_at"]) if data.get("received_at") else None
            def since(name):
                return event is not None and name in state.baseline_events and event >= state.baseline_events[name]
            oi_change = oi-state.baseline_oi if oi is not None and state.baseline_oi is not None and since("oi") else None
            price_change = price-state.baseline_price if price is not None and state.baseline_price is not None and since("ltp") else None
            return {**contract.public(), **data, "oi_session_baseline": state.baseline_oi,
                    "baseline_at": state.baseline_at, "previous_close_oi": previous_oi,
                    "oi_change_session": oi_change, "oi_change_intraday": oi_change,
                    "oi_change_vs_previous_close": oi-previous_oi if oi is not None and previous_oi is not None else None,
                    "oi_change_percent": 100*oi_change/state.baseline_oi if oi_change is not None and state.baseline_oi else None,
                    "price_change": price_change,
                    "price_change_percent": 100*price_change/state.baseline_price if price_change is not None and state.baseline_price else None,
                    "volume_change": volume-state.baseline_volume if volume is not None and state.baseline_volume is not None and volume >= state.baseline_volume and since("volume") else None,
                    "change_basis": "first_valid_observation_after_login_or_session_open"}
