from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from math import isfinite
from threading import RLock

from app.analytics.session import local


def expiry_date(value):
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(value) if isinstance(value, str) else value


@dataclass(frozen=True)
class OptionContract:
    index: str
    expiry: date
    strike: float
    option_type: str
    lot_size: int
    instrument_token: int
    trading_symbol: str

    def public(self):
        return {**asdict(self), "expiry": self.expiry.isoformat()}


class OptionDiscovery:
    def __init__(self, resolver):
        self.resolver = resolver
        self.lock = RLock()
        self.contracts = ()
        self.futures = ()
        self.ready = False

    def refresh(self, client):
        now = local(self.resolver.clock())
        rows = self.resolver.find(client, "NFO")
        contracts, futures, seen, tokens, symbols = [], [], set(), set(), set()
        for row in rows:
            if row.get("name") not in ("NIFTY", "BANKNIFTY"):
                continue
            expiry = expiry_date(row.get("expiry"))
            if not isinstance(expiry, date) or expiry < now.date() or (expiry == now.date() and now.time() >= time(15, 30)):
                continue
            if row.get("instrument_type") == "FUT" and row.get("segment") == "NFO-FUT":
                futures.append({**row, "expiry": expiry})
            if row.get("instrument_type") not in ("CE", "PE") or row.get("segment") != "NFO-OPT":
                continue
            strike = float(row["strike"])
            if not isfinite(strike) or strike <= 0 or int(row["lot_size"]) <= 0 or int(row["instrument_token"]) <= 0:
                continue
            key = (row["name"], expiry, strike, row["instrument_type"])
            if key in seen or int(row["instrument_token"]) in tokens or row["tradingsymbol"] in symbols:
                raise ValueError("Ambiguous options metadata")
            seen.add(key)
            tokens.add(int(row["instrument_token"]))
            symbols.add(row["tradingsymbol"])
            contracts.append(OptionContract(*key, int(row["lot_size"]), int(row["instrument_token"]), row["tradingsymbol"]))
        with self.lock:
            self.contracts, self.futures = tuple(contracts), tuple(futures)
            self.ready = True

    def chain(self, index, expiry):
        with self.lock:
            return sorted((c for c in self.contracts if c.index == index and c.expiry == expiry and self._active(c.expiry)), key=lambda c: (c.strike, c.option_type))

    def _active(self, expiry):
        now = local(self.resolver.clock())
        return expiry > now.date() or (expiry == now.date() and now.time() < time(15, 30))

    def get_expiries(self, index):
        with self.lock:
            return sorted({c.expiry for c in self.contracts if c.index == index and self._active(c.expiry)})

    def get_option_contract(self, index, expiry, strike, option_type):
        return next((c for c in self.chain(index, expiry) if c.strike == strike and c.option_type == option_type), None)

    def selections(self, index):
        expiries = self.get_expiries(index)
        with self.lock:
            monthly = sorted({f["expiry"] for f in self.futures if f["name"] == index and f["expiry"] in expiries})
        return {"nearest": expiries[0] if expiries else None, "next": expiries[1] if len(expiries) > 1 else None,
                "monthly": monthly[0] if monthly else None}

    def matching_future(self, index, expiry):
        with self.lock:
            matches = [f for f in self.futures if f["name"] == index and f["expiry"] == expiry]
        return matches[0] if len(matches) == 1 else None


def atm(strikes, spot):
    return min(strikes, key=lambda strike: (abs(strike - spot), strike)) if strikes and spot is not None and isfinite(spot) else None


class SubscriptionWindow:
    """Shift after two strike steps; retain one-step movement to avoid boundary churn."""
    def __init__(self, radius=10, hysteresis=2):
        self.radius, self.hysteresis = radius, hysteresis
        self.centers = {}

    def select(self, index, expiry, contracts, spot):
        strikes = sorted({c.strike for c in contracts})
        current = atm(strikes, spot)
        if current is None:
            return []
        key = (index, expiry)
        previous = self.centers.get(key)
        if previous not in strikes or abs(strikes.index(current) - strikes.index(previous)) >= self.hysteresis:
            previous = current
            self.centers[key] = current
        position = strikes.index(previous)
        allowed = set(strikes[max(0, position-self.radius):position+self.radius+1])
        return [c for c in contracts if c.strike in allowed]
