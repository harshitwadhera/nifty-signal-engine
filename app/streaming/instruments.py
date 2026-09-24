from copy import deepcopy
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
