from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite
from threading import RLock

from app.analytics.session import INTERVALS, bounds, local


class CandleAggregator:
    """Five-second event-time lateness window; closed bars are immutable to live ticks."""

    def __init__(self, store, lateness_seconds=5):
        self.store = store
        self.lock = RLock()
        self.lateness = timedelta(seconds=lateness_seconds)
        self.active = {}
        self.seen = OrderedDict()
        self.volumes = {}
        self.watermark = None
        self.rejected_late = 0

    def consume(self, tick):
        timestamp, received = local(tick.timestamp), local(tick.received_at)
        if timestamp > received + timedelta(seconds=5) or not bounds(timestamp, "1m") or not isfinite(tick.last_price):
            return False
        # Index timestamps have second precision: identical snapshots in that second
        # cannot be distinguished from retransmits. Receipt-time-only ticks stay distinct.
        identity = (tick.instrument_token, timestamp, tick.last_price, tick.volume, tick.ohlc)
        with self.lock:
            if self.watermark and timestamp < self.watermark:
                self.rejected_late += 1
                return False
            if identity in self.seen:
                return False
            self.seen[identity] = None
            while len(self.seen) > 10000:
                self.seen.popitem(last=False)
            delta = None
            volume_key = (tick.symbol, timestamp.date())
            previous = self.volumes.get(volume_key)
            if tick.volume is not None:
                if previous and timestamp >= previous[0] and tick.volume >= previous[1]:
                    delta = tick.volume - previous[1]
                if not previous or timestamp >= previous[0]:
                    self.volumes[volume_key] = (timestamp, tick.volume)
            for interval in INTERVALS:
                start, end = bounds(timestamp, interval)
                key = (tick.symbol, interval, start.isoformat())
                if key not in self.active:
                    self.active[key] = {"symbol": tick.symbol, "interval": interval,
                        "start_time": start.isoformat(), "end_time": end.isoformat(),
                        "open": tick.last_price, "high": tick.last_price, "low": tick.last_price,
                        "close": tick.last_price, "volume": 0 if tick.volume == 0 else None, "tick_count": 0,
                        "completed": False, "partial": timestamp > start + timedelta(seconds=5),
                        "source": "live", "_first": timestamp, "_last": timestamp, "_minutes": set()}
                candle = self.active[key]
                candle.setdefault("_minutes", set()).add(timestamp.replace(second=0, microsecond=0))
                candle["high"] = max(candle["high"], tick.last_price)
                candle["low"] = min(candle["low"], tick.last_price)
                if timestamp < candle["_first"]:
                    candle["open"], candle["_first"] = tick.last_price, timestamp
                if timestamp >= candle["_last"]:
                    candle["close"], candle["_last"] = tick.last_price, timestamp
                if candle["tick_count"] is not None:
                    candle["tick_count"] += 1
                # A cumulative delta spanning candle boundaries cannot be allocated reliably.
                if delta is not None and previous and start <= previous[0] <= timestamp:
                    if candle["volume"] is not None:
                        candle["volume"] += delta
                elif tick.volume is not None:
                    candle["volume"] = None
            self.advance(received)
            return True

    @staticmethod
    def public(candle):
        return {k: deepcopy(v) for k, v in candle.items() if not k.startswith("_")}

    def advance(self, now):
        with self.lock:
            watermark = local(now) - self.lateness
            self.watermark = max(self.watermark, watermark) if self.watermark else watermark
            ready = [k for k, c in self.active.items() if datetime.fromisoformat(c["end_time"]) <= self.watermark]
            for key in ready:
                candle = self.active[key]
                expected = int((datetime.fromisoformat(candle["end_time"]) - datetime.fromisoformat(candle["start_time"])).total_seconds() // 60)
                candle["partial"] = candle["partial"] or len(candle.get("_minutes", ())) < expected
            candles = [{**self.public(self.active[k]), "completed": True} for k in ready]
            self.store.save(candles)
            for key in ready:
                del self.active[key]
            self.volumes = {k: v for k, v in self.volumes.items() if k[1] == local(now).date()}

    def candles(self, symbol, interval, limit=100):
        with self.lock:
            rows = self.store.load(symbol, interval, limit)
            rows += [self.public(c) for c in self.active.values() if c["symbol"] == symbol and c["interval"] == interval]
            return sorted(rows, key=lambda c: c["start_time"])[-limit:]

    def mark_gap(self):
        with self.lock:
            for candle in self.active.values():
                candle["partial"] = True
                candle["volume"] = None
            self.volumes.clear()

    def import_minutes(self, symbol, minutes, now, has_volume=False):
        """Use completed historical minutes only; seed current larger bars where possible."""
        now = local(now)
        groups = {}
        for row in minutes:
            ts = local(row["date"])
            if not bounds(ts, "1m") or ts + timedelta(minutes=1) > now - self.lateness:
                continue
            if any(not isinstance(row.get(k), (int, float)) or not isfinite(row[k]) for k in ("open", "high", "low", "close")):
                continue
            for interval in INTERVALS:
                start, end = bounds(ts, interval)
                groups.setdefault((interval, start, end), {})[ts] = row
        completed = []
        with self.lock:
            for (interval, start, end), mapping in groups.items():
                rows = sorted(mapping.items())
                expected = int((end - start).total_seconds() // 60)
                volume = sum(r["volume"] for _, r in rows) if has_volume and all(isinstance(r.get("volume"), (int, float)) and r["volume"] >= 0 for _, r in rows) else None
                c = {"symbol": symbol, "interval": interval, "start_time": start.isoformat(), "end_time": end.isoformat(),
                     "open": rows[0][1]["open"], "high": max(r["high"] for _, r in rows),
                     "low": min(r["low"] for _, r in rows), "close": rows[-1][1]["close"],
                     "volume": volume, "tick_count": None, "completed": end <= now - self.lateness,
                     "partial": len(rows) != expected, "source": "historical"}
                if c["completed"]:
                    completed.append(c)
                else:
                    key = (symbol, interval, start.isoformat())
                    current = self.active.get(key)
                    history_end = rows[-1][0] + timedelta(minutes=1)
                    if current is None:
                        c.update(_first=rows[0][0], _last=history_end, _minutes=set(mapping), source="mixed")
                        c["partial"] = rows[0][0] != start or len(rows) != int((history_end - start).total_seconds() // 60)
                        self.active[key] = c
                    elif current["_first"] > rows[0][0]:
                        current["open"] = c["open"]
                        current["high"] = max(c["high"], current["high"])
                        current["low"] = min(c["low"], current["low"])
                        current["partial"] = rows[0][0] != start or current["_first"] > history_end + self.lateness
                        if history_end > current["_last"]:
                            current["close"] = c["close"]
                            current["_last"] = history_end
                        current["volume"] = None  # No reliable cumulative baseline at the join.
                        current["tick_count"] = None
                        current["_first"] = rows[0][0]
                        current.setdefault("_minutes", set()).update(mapping)
                        current["source"] = "mixed"
            self.store.save(completed)
            # Authoritative historical bars supersede any still-buffered live counterpart.
            for c in completed:
                self.active.pop((symbol, c["interval"], c["start_time"]), None)
