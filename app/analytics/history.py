from datetime import datetime, time, timedelta
from threading import Lock

from app.analytics.session import IST, local, bounds


class HistoryRecovery:
    """Calls are made only from the background recovery worker, not HTTP/tick callbacks."""

    def __init__(self, store, aggregator, pause=lambda: None):
        self.store, self.aggregator, self.pause = store, aggregator, pause
        self.lock = Lock()
        self.recovered = {}

    def recover(self, client, instrument, now):
        now = local(now)
        symbol = instrument["trading_symbol"]
        token = instrument["instrument_token"]
        with self.lock:
            previous = self.store.previous(symbol, now.date())
            if previous is None:
                if self.pause():
                    return previous
                rows = client.historical_data(token, now.date() - timedelta(days=45),
                                              now.date() - timedelta(days=1), "day")
                valid = [r for r in rows if local(r["date"]).date() < now.date() and local(r["date"]).weekday() < 5]
                latest = max(valid, key=lambda r: r["date"]) if valid else None
                previous = ({"session_date": local(latest["date"]).date().isoformat(),
                             "high": latest["high"], "low": latest["low"], "close": latest["close"]}
                            if latest else {"session_date": None, "high": None, "low": None, "close": None})
                self.store.previous(symbol, now.date(), previous)
            key = (symbol, now.date())
            # Load existing session context before fetching gaps. SQL remains the history source.
            stored = self.store.load(symbol, "1m", 1000, now.date())
            prior = self.recovered.get(key)
            opening = datetime.combine(now.date(), time(9, 15), IST)
            closing = datetime.combine(now.date(), time(15, 30), IST)
            cutoff = min((now - timedelta(seconds=5)).replace(second=0, microsecond=0), closing)
            known = {datetime.fromisoformat(c["start_time"]) for c in stored if not c["partial"] and c["source"] == "historical"}
            missing = []
            cursor = opening
            while cursor < cutoff and now.weekday() < 5:
                if cursor not in known:
                    missing.append(cursor)
                cursor += timedelta(minutes=1)
            warm = all(len([c for c in self.store.load(symbol, interval, 1000) if not c["partial"]]) >= 20
                       for interval in ("5m", "15m", "30m"))
            if prior and not missing:
                return previous
            if not prior and warm and not missing:
                self.recovered[key] = now
                return previous
            if missing and (prior or warm):
                start = bounds(missing[0], "30m")[0]
            else:
                start = prior - timedelta(minutes=31) if prior else datetime.combine(now.date() - timedelta(days=10), time(9, 15), IST)
            if self.pause():
                return previous
            rows = client.historical_data(token, start, now, "minute")
            self.aggregator.import_minutes(symbol, rows, now, has_volume="alias" in instrument)
            self.recovered[key] = now
            return previous
