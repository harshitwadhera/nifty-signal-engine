from copy import deepcopy
from datetime import date, datetime, time
from threading import RLock

from app.session import now_ist

INDEX_NAMES = {"NIFTY 50": "NIFTY 50", "NIFTY BANK": "BANK NIFTY", "INDIA VIX": "INDIA VIX"}


class InstrumentResolver:
    """Daily exchange metadata cache; preserve derivative metadata for future queries."""

    def __init__(self, clock=now_ist):
        self.clock = clock
        self._cache = {}
        self._lock = RLock()

    def find(self, client, exchange, **criteria):
        with self._lock:
            day = self.clock().date()
            cached_day, rows = self._cache.get(exchange, (None, []))
            if cached_day != day:
                rows = client.instruments(exchange)
                if not isinstance(rows, list):
                    raise ValueError("Invalid instrument metadata")
                self._cache[exchange] = (day, deepcopy(rows))
            return deepcopy([row for row in rows if row.get("exchange") == exchange
                             and all(row.get(key) == value for key, value in criteria.items())])

    def invalidate(self, exchange):
        with self._lock:
            self._cache.pop(exchange, None)

    def indices(self, client):
        instruments = []
        for symbol, friendly in INDEX_NAMES.items():
            rows = self.find(client, "NSE", tradingsymbol=symbol, segment="INDICES")
            if len(rows) != 1:
                raise ValueError("Required index metadata missing or ambiguous")
            token = int(rows[0]["instrument_token"])
            if token <= 0 or any(row["instrument_token"] == token for row in instruments):
                raise ValueError("Invalid index token")
            instruments.append({"instrument_token": token, "trading_symbol": symbol,
                                "friendly_name": friendly})
        return instruments

    def universe(self, client):
        instruments = self.indices(client)
        now = self.clock()
        for underlying in ("NIFTY", "BANKNIFTY"):
            rows = self.find(client, "NFO", name=underlying, instrument_type="FUT", segment="NFO-FUT")
            candidates = []
            for row in rows:
                expiry = row.get("expiry")
                if isinstance(expiry, datetime):
                    expiry = expiry.date()
                if isinstance(expiry, str):
                    expiry = date.fromisoformat(expiry)
                if isinstance(expiry, date) and (expiry > now.date() or
                        (expiry == now.date() and now.time() < time(15, 30))):
                    candidates.append((expiry, row))
            if not candidates:
                raise ValueError("No valid front-month futures contract")
            nearest = min(expiry for expiry, _ in candidates)
            matches = [row for expiry, row in candidates if expiry == nearest]
            if len(matches) != 1:
                raise ValueError("Ambiguous futures metadata")
            row = matches[0]
            token = int(row["instrument_token"])
            if token <= 0 or any(i["instrument_token"] == token for i in instruments):
                raise ValueError("Invalid futures token")
            instruments.append({"instrument_token": token, "trading_symbol": row["tradingsymbol"],
                                "friendly_name": underlying + " FUT", "alias": underlying + "_FUT",
                                "expiry": nearest.isoformat()})
        return instruments
