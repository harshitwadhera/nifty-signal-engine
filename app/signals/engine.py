"""Deterministic descriptive evidence scores, not probabilities or trade advice.

No clocks, I/O, state, SDK classes or downstream lifecycle dependencies.
"""
from math import isfinite
from dataclasses import replace

from app.options.quality import BEHAVIOR, atm_quality

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
        weights = [c.options_weight*p for p in c.options_parts]
        components = {}

        def balanced_fraction(quality):
            """Proportionally scale broad-chain evidence; never use a hard coverage cutoff."""
            quality = quality or {}
            fractions = []
            for side in ("call", "put"):
                observed = number(quality.get(side))
                expected = number(quality.get(side+"_expected"))
                if observed is None or expected is None or observed < 0 or expected <= 0:
                    return 0.0
                fractions.append(min(1.0, observed/expected))
            return min(fractions)

        def add(key, label, votes, weight, maximum, reason, **details):
            t.add(label, votes, weight)
            components[key] = dict(available_weight=round(weight if votes else 0, 6),
                                   maximum_weight=round(maximum, 6), reason=reason, **details)

        aggregate_fresh = data.get("evidence_fresh") is True

        # Positioning uses every individually usable observation, but its evidence
        # budget is proportional to the weaker CALL/PUT population. One fresh pair
        # cannot claim the same 10.5-point budget as a broadly observed chain.
        flow_quality = data.get("positioning_quality") or {}
        flow_fraction = balanced_fraction(flow_quality) if aggregate_fresh else 0.0
        flow_ready = flow_fraction > 0
        flow = []
        for side, writing_sign in (("put", 1), ("call", -1)):
            for suffix, sign in (("writing_zones", writing_sign), ("unwinding_zones", -writing_sign)):
                rows = (data.get(side+"_"+suffix) or []) if flow_ready else []
                valid = [r for r in rows if positive(r.get("strike")) is not None and
                         number(r.get("oi_change_session" if suffix == "writing_zones" else "value")) is not None]
                valid = [r for r in valid if (r.get("oi_change_session", 0) > 0 if suffix == "writing_zones" else r["value"] < 0)]
                if valid:
                    flow.append(sign)
                    t.evidence.append(f"{side} {suffix}: {len(valid)} levels")
            addition = data.get("highest_"+side+"_oi_addition") or {}
            if positive(addition.get("value")) is not None:
                t.evidence.append(f"{side} OI additions present; directional only with price/positioning")
        flow_weight = weights[0]*flow_fraction
        add("positioning_flow", "Positioning flow", (flow or [0]) if flow_ready else [], flow_weight, weights[0],
            (f"Balanced usable CALL/PUT positioning population {100*flow_fraction:.1f}%"
             if flow_ready else "Fresh price/OI positioning required on both CALL and PUT sides"),
            usable_call_observations=number(flow_quality.get("call")) or 0,
            usable_put_observations=number(flow_quality.get("put")) or 0,
            expected_call_observations=number(flow_quality.get("call_expected")) or 0,
            expected_put_observations=number(flow_quality.get("put_expected")) or 0)

        # ATM remains deliberately independent of broad-chain population.
        atm_votes = []
        atm_details = {}
        for side, sign in (("ce", 1), ("pe", -1)):
            row = data.get("atm_"+side) or {}
            atm_details[side] = atm_quality(row, c.max_iv)
            if atm_details[side]["usable"]:
                atm_votes.append(sign*BEHAVIOR[row["positioning"]])
        add("atm_behavior", "Liquid ATM CE/PE behavior", atm_votes, weights[1]*len(atm_votes)/2, weights[1],
            f"{len(atm_votes)} of 2 ATM sides usable", sides=atm_details)

        # OI walls remain observable with an incomplete chain, but their budget is
        # proportional to balanced fresh OI coverage on both sides.
        spot = positive(data.get("spot"))
        call, put = positive(data.get("call_oi_wall")), positive(data.get("put_oi_wall"))
        oi_quality = data.get("oi_quality") or {}
        wall_fraction = balanced_fraction(oi_quality) if aggregate_fresh else 0.0
        wall_votes = self.range_vote(spot, call, put) if wall_fraction > 0 else []
        wall_weight = weights[2]*wall_fraction
        add("oi_wall_breakout", "OI wall breakout", wall_votes, wall_weight, weights[2],
            (f"Balanced usable CALL/PUT OI population {100*wall_fraction:.1f}%"
             if wall_votes else "Insufficient valid OI-wall data or range"),
            usable_call_observations=number(oi_quality.get("call")) or 0,
            usable_put_observations=number(oi_quality.get("put")) or 0,
            expected_call_observations=number(oi_quality.get("call_expected")) or 0,
            expected_put_observations=number(oi_quality.get("put_expected")) or 0)

        # PCR is still confirmation-only. Each ratio gets at most one third of the
        # 6-point budget, scaled by its own matched-strike population. No hard
        # percentage threshold is introduced.
        ratio_quality = data.get("pcr_quality") or {}
        anchor = t.bull-t.bear
        pcr_available = 0.0
        usable_ratios = 0
        ratio_details = {}
        for field, label in (("pcr_oi", "OI"), ("pcr_volume", "Volume"), ("near_atm_pcr_oi", "Near-ATM OI")):
            value = number(data.get(field)) if aggregate_fresh else None
            quality = ratio_quality.get(field) or {}
            paired = number(quality.get("paired_strikes"))
            population = number(quality.get("population_pairs"))
            fraction = (min(1.0, paired/population)
                        if paired is not None and population is not None and paired > 0 and population > 0 else 0.0)
            available = value is not None and value >= 0 and fraction > 0
            ratio_details[field] = {"paired_strikes": paired or 0, "population_pairs": population or 0,
                                    "population_fraction": round(fraction, 6), "available": available}
            if not available:
                continue
            usable_ratios += 1
            vote = 1 if value > c.pcr_bullish else -1 if value < c.pcr_bearish else 0
            aligned = vote if anchor*vote > 0 else 0
            if anchor*vote < 0:
                t.conflicts.append("PCR conflicts with non-PCR positioning")
            ratio_weight = weights[3]/3*fraction
            t.add(f"PCR confirmation ({label}; non-PCR anchor required)", [aligned], ratio_weight)
            pcr_available += ratio_weight
        components["pcr_confirmation"] = dict(
            available_weight=round(pcr_available, 6), maximum_weight=round(weights[3], 6),
            reason=f"{usable_ratios} of 3 ratios usable; weight scaled by matched-strike population",
            usable_ratios=usable_ratios, ratios=ratio_details)

        if positive(data.get("max_pain")) is not None:
            t.evidence.append("Max pain available as secondary context; no directional points")
        return replace(t.result(), component_availability=components)

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

    def breadth(self, data):
        t, c = Tally(self.config), self.config
        coverage = number(data.get("coverage_percent"))
        if data.get("stale") or coverage is None or coverage < c.minimum_breadth_coverage:
            return t.result()
        for field, fraction in zip(("percent_positive", "percent_above_5m_ema20", "percent_above_15m_ema20"), c.breadth_parts):
            value = number(data.get(field))
            field_coverage = number(data.get(field+"_coverage", coverage))
            if value is not None and 0 <= value <= 100 and field_coverage is not None and field_coverage >= c.minimum_breadth_coverage:
                # For A/D, unchanged is neutral, not evidence of declines.
                negative = number(data.get("percent_negative")) if field == "percent_positive" else 100-value
                if negative is None or not 0 <= negative <= 100 or (field == "percent_positive" and value+negative > 100.000001):
                    continue
                t.add(field, [1 if value >= c.breadth_positive else -1 if negative >= 100-c.breadth_negative else 0], c.breadth_weight*fraction)
        t.evidence.append("Breadth weighting: " + str(data.get("weighting", "unweighted")))
        return t.result()

    def decide(self, snapshot: SignalInput):
        from .decision import decide
        return decide(snapshot, self.score(snapshot), self.config)

    def score(self, snapshot: SignalInput) -> ScoreResult:
        categories = {"price_trend": self.price(snapshot.structure), "options_positioning": self.options(snapshot.options),
                      "breadth_constituents": self.breadth(snapshot.breadth),
                      "volatility": self.volatility(snapshot.volatility), "futures_structure": self.futures(snapshot.structure)}
        weights = dict(price_trend=self.config.price_weight, options_positioning=self.config.options_weight,
                       breadth_constituents=self.config.breadth_weight, volatility=self.config.volatility_weight,
                       futures_structure=self.config.futures_weight)
        categories = {name: replace(category, maximum_weight=weights[name]) for name, category in categories.items()}
        return ScoreResult(snapshot.index_name, snapshot.as_of, categories,
                           round(sum(c.bullish_points for c in categories.values()), 6),
                           round(sum(c.bearish_points for c in categories.values()), 6),
                           round(sum(c.available_weight for c in categories.values()), 6), self.config)
