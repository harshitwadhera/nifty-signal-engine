"""Deterministic descriptive evidence scores, not probabilities or trade advice.

No clocks, I/O, state, SDK classes or downstream lifecycle dependencies.
"""
from math import isfinite

from .models import CategoryScore, ScoreResult, SignalConfig, SignalInput


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def positive(value):
    value = number(value)
    return value if value is not None and value > 0 else None


class Tally:
    def __init__(self, config):
        self.config = config
        self.bull = self.bear = self.available = 0.0
        self.evidence, self.conflicts = [], []

    def add(self, label, votes, weight):
        if not votes:
            return
        self.available += weight
        self.bull += weight * sum(v > 0 for v in votes)/len(votes)
        self.bear += weight * sum(v < 0 for v in votes)/len(votes)
        self.evidence.append(f"{label}: bullish={votes.count(1)}, bearish={votes.count(-1)}, neutral={votes.count(0)}")
        if 1 in votes and -1 in votes:
            self.conflicts.append(f"{label}: opposing observations")

    def result(self):
        net = self.bull-self.bear
        direction = ("unavailable" if not self.available else "neutral" if abs(net) <= self.available*self.config.direction_margin
                     else "bullish" if net > 0 else "bearish")
        conflicts = self.conflicts + (["Category contains both bullish and bearish evidence"] if self.bull and self.bear else [])
        return CategoryScore(direction, round(self.bull, 6), round(self.bear, 6), round(self.available, 6),
                             tuple(self.evidence), tuple(conflicts))


class SignalEngine:
    def __init__(self, config=None):
        self.config = config or SignalConfig()

    def compare(self, a, b):
        a, b = positive(a), positive(b)
        if a is None or b is None:
            return []
        delta = (a/b-1)*100
        return [1 if delta > self.config.price_deadband_percent else -1 if delta < -self.config.price_deadband_percent else 0]

    def range_vote(self, spot, high, low):
        spot, high, low = positive(spot), positive(high), positive(low)
        if None in (spot, high, low) or high < low:
            return []
        return [1 if self.compare(spot, high) == [1] else -1 if self.compare(spot, low) == [-1] else 0]

    def price(self, data):
        t, c = Tally(self.config), self.config
        if data.get("stale"):
            return t.result()
        spot = positive(data.get("spot"))
        parts = [c.price_weight*p for p in c.price_parts]
        for label, prefix, weight in (("Opening range", "opening_range", parts[0]), ("Previous range", "previous_day", parts[1])):
            t.add(label, self.range_vote(spot, data.get(prefix+"_high"), data.get(prefix+"_low")), weight)
        t.add("Previous close", self.compare(spot, data.get("previous_day_close")), parts[2])
        high, low = positive(data.get("day_high")), positive(data.get("day_low"))
        if spot is not None and high is not None and low is not None and high > low and low <= spot <= high:
            location = (spot-low)/(high-low)
            t.add("Day range position", [1 if location >= c.day_upper_fraction else -1 if location <= c.day_lower_fraction else 0], parts[3])
        votes = []
        for interval in ("5m", "15m", "30m"):
            row = data.get(interval) or {}
            if row.get("stale") or row.get("partial"):
                continue
            vote = self.compare(row.get("ema9"), row.get("ema20"))
            if vote:
                votes += vote
                t.evidence.append(f"{interval} EMA9/EMA20: {vote[0]}")
        # Fixed shared budget: additional correlated intervals cannot multiply it.
        t.add("EMA consensus", votes, parts[4]*len(votes)/3)
        return t.result()

    def options(self, data):
        t, c = Tally(self.config), self.config
        if data.get("stale"):
            return t.result()
        weights = [c.options_weight*p for p in c.options_parts]
        flow = []
        for side, writing_sign in (("put", 1), ("call", -1)):
            # Additions alone do not identify buyers versus writers. Writing and
            # unwinding are correlated OI evidence and share one fixed budget.
            for suffix, sign in (("writing_zones", writing_sign), ("unwinding_zones", -writing_sign)):
                rows = data.get(side+"_"+suffix) or []
                valid = [r for r in rows if positive(r.get("strike")) is not None and
                         number(r.get("oi_change_session" if suffix == "writing_zones" else "value")) is not None]
                valid = [r for r in valid if (r.get("oi_change_session", 0) > 0 if suffix == "writing_zones" else r["value"] < 0)]
                if valid:
                    flow.append(sign)
                    t.evidence.append(f"{side} {suffix}: {len(valid)} levels")
            addition = data.get("highest_"+side+"_oi_addition") or {}
            if positive(addition.get("value")) is not None:
                t.evidence.append(f"{side} OI additions present; directional only with price/positioning")
        t.add("Positioning flow", flow, weights[0])
        atm_votes = []
        behavior = {"LONG_BUILDUP": 1, "SHORT_COVERING": 1, "SHORT_BUILDUP": -1, "LONG_UNWINDING": -1, "NEUTRAL": 0}
        for side, sign in (("ce", 1), ("pe", -1)):
            row = data.get("atm_"+side) or {}
            iv = number(row.get("iv"))
            if row.get("stale") or row.get("liquidity_state") not in ("LIQUID", "MODERATE") or (row.get("iv") is not None and (iv is None or not 0 < iv <= c.max_iv)):
                continue
            if row.get("positioning") in behavior:
                atm_votes.append(sign*behavior[row["positioning"]])
        t.add("Liquid ATM CE/PE behavior", atm_votes, weights[1]*len(atm_votes)/2)
        spot = positive(data.get("spot"))
        call, put = positive(data.get("call_oi_wall")), positive(data.get("put_oi_wall"))
        # Inside the walls is neutral; wall distance is not an independent trend.
        t.add("OI wall breakout", self.range_vote(spot, call, put), weights[2])
        pcr_votes = []
        for field in ("pcr_oi", "pcr_volume", "near_atm_pcr_oi"):
            value = number(data.get(field))
            if value is not None and value >= 0:
                pcr_votes.append(1 if value > c.pcr_bullish else -1 if value < c.pcr_bearish else 0)
        if pcr_votes:
            # Ratios confirm a non-PCR direction, never establish one or reverse it.
            anchor = t.bull-t.bear
            aligned = [v if anchor*v > 0 else 0 for v in pcr_votes]
            if any(anchor*v < 0 for v in pcr_votes):
                t.conflicts.append("PCR conflicts with non-PCR positioning")
            t.add("PCR confirmation (non-PCR anchor required)", aligned, weights[3]*len(pcr_votes)/3)
        if positive(data.get("max_pain")) is not None:
            t.evidence.append("Max pain available as secondary context; no directional points")
        return t.result()

    def futures(self, data):
        t, c = Tally(self.config), self.config
        if data.get("stale"):
            return t.result()
        t.add("Future vs VWAP", self.compare(data.get("future"), data.get("futures_vwap")), c.futures_weight*c.futures_parts[0])
        moves = [number(data.get(k)) for k in ("future_change_percent", "spot_change_percent")]
        if all(v is not None for v in moves):
            votes = [1 if v > c.move_deadband_percent else -1 if v < -c.move_deadband_percent else 0 for v in moves]
            t.add("Spot/future direction confirmation", votes if votes[0] == votes[1] else [0], c.futures_weight*c.futures_parts[1])
            if votes[0]*votes[1] < 0:
                t.conflicts.append("Spot and futures move in opposing directions")
        basis = number(data.get("futures_basis"))
        if basis is not None:
            t.evidence.append(f"Futures basis={basis}; carry/expiry context only, no directional points")
        return t.result()

    def volatility(self, data):
        t, c = Tally(self.config), self.config
        level = positive(data.get("level"))
        if data.get("stale") or level is None:
            return t.result()
        t.add("VIX risk context (non-directional)", [0], c.volatility_weight)
        t.evidence.append("VIX regime: " + ("elevated" if level >= c.vix_high else "low" if level <= c.vix_low else "normal"))
        change = number(data.get("change_percent"))
        if change is not None:
            t.evidence.append("VIX change: " + ("rising" if change > c.vix_change_percent else "falling" if change < -c.vix_change_percent else "stable"))
        return t.result()

    def score(self, snapshot: SignalInput) -> ScoreResult:
        categories = {"price_trend": self.price(snapshot.structure), "options_positioning": self.options(snapshot.options),
                      "breadth_constituents": CategoryScore("unavailable", 0, 0, 0, ("Breadth scoring reserved",), ()),
                      "volatility": self.volatility(snapshot.volatility), "futures_structure": self.futures(snapshot.structure)}
        return ScoreResult(snapshot.index_name, snapshot.as_of, categories,
                           round(sum(c.bullish_points for c in categories.values()), 6),
                           round(sum(c.bearish_points for c in categories.values()), 6),
                           round(sum(c.available_weight for c in categories.values()), 6), self.config)
