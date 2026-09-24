from datetime import datetime, timezone
from math import isfinite
from typing import Protocol

SYMBOLS = ("NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX")


class MarketDataProvider(Protocol):
    """Future WebSocket cache adapters can implement the same snapshot contract."""

    def snapshot(self, client) -> dict: ...


class RestMarketDataProvider:
    def snapshot(self, client):
        quotes = client.quote(list(SYMBOLS))
        instruments = []
        for symbol in SYMBOLS:
            quote = quotes.get(symbol, {})
            price = quote.get("last_price")
            valid = isinstance(price, (int, float)) and not isinstance(price, bool) and isfinite(price)
            timestamp = quote.get("timestamp")
            instruments.append({"symbol": symbol, "name": symbol.split(":", 1)[1],
                                "value": price if valid else None,
                                "quote_timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else None,
                                "available": valid})
        return {"connection_status": "connected", "instruments": instruments,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "partial": any(not item["available"] for item in instruments)}
