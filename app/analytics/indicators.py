from math import isfinite


def ema(values, period):
    """SMA seed, then recursive EMA; unavailable before the full warm-up."""
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for close in values[period:]:
        value = close * alpha + value * (1 - alpha)
    return value


def momentum(candles):
    completed = [c for c in candles if c["completed"] and not c["partial"]]
    closes = [c["close"] for c in completed]
    short, long = ema(closes, 9), ema(closes, 20)
    close = closes[-1] if closes else None
    return {"ema9": short, "ema20": long,
            "price_above_ema9": close > short if close is not None and short is not None else None,
            "price_above_ema20": close > long if close is not None and long is not None else None,
            "ema9_above_ema20": short > long if short is not None and long is not None else None,
            "as_of": completed[-1]["end_time"] if completed else None,
            "price_basis": "last_completed_spot_candle_close"}


class VWAP:
    """Futures only. Volumes are cumulative, never summed as per-tick quantities."""

    def __init__(self):
        self.day = self.last_time = self.volume = None
        self.turnover = 0.0
        self.observed_volume = 0
        self.value = None
        self.method = None

    def update(self, tick):
        from app.analytics.session import local
        day, timestamp = local(tick.timestamp).date(), tick.timestamp
        if self.day != day:
            self.__init__()
            self.day = day
        if self.last_time and timestamp < self.last_time:
            return
        self.last_time = timestamp
        volume = tick.volume
        if volume is None or volume < 0 or (self.volume is not None and volume < self.volume):
            self.volume = volume
            self.value = self.method = None
            self.turnover = self.observed_volume = 0
            return
        delta = volume - self.volume if self.volume is not None else 0
        self.volume = volume
        if volume <= 0:
            self.value = self.method = None
            return
        average = tick.average_traded_price
        if average is not None and isfinite(average) and average > 0:
            # Exchange cumulative turnover = session ATP * cumulative volume.
            self.value = (average * volume) / volume
            self.method = "exchange_session_average"
        else:
            self.turnover += tick.last_price * delta
            self.observed_volume += delta
            self.value = self.turnover / self.observed_volume if self.observed_volume else None
            self.method = "observed_volume_deltas" if self.value is not None else None

    def snapshot(self, price):
        return {"price": price, "vwap": self.value, "vwap_method": self.method,
                "full_session": self.method == "exchange_session_average",
                "price_vs_vwap": None if self.value is None or price is None else
                    "above" if price > self.value else "below" if price < self.value else "at"}
