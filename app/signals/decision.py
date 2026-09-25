"""Fail-closed decision from one supplied snapshot; no entry/lifecycle logic."""
from datetime import datetime
from dataclasses import asdict

from .engine import number, positive
from .models import DecisionResult


def fresh(source, key, as_of, limit):
    if source.get("stale") is not False:
        return False
    try:
        observed = datetime.fromisoformat(source[key])
        now = datetime.fromisoformat(as_of)
        return observed.utcoffset() is not None and now.utcoffset() is not None and 0 <= (now-observed).total_seconds() <= limit
    except (ValueError, TypeError, KeyError):
        return False


def decide(snapshot, scores, config):
    bull, bear = scores.bullish_points, scores.bearish_points
    winner = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    aligned = tuple(k for k, c in scores.categories.items() if c.direction == winner and winner != "neutral")
    issues = []
    quality = {name: fresh(source, timestamp, snapshot.as_of, config.critical_max_age_seconds)
               for name, source, timestamp in (
                   ("structure_fresh", snapshot.structure, "as_of"),
                   ("options_fresh", snapshot.options, "last_full_chain_refresh_at"),
                   ("breadth_fresh", snapshot.breadth, "as_of"),
                   ("vix_fresh", snapshot.volatility, "as_of"))}
    if not all(quality.values()):
        issues.append("Critical data stale or freshness unavailable")
    quality["critical_values_present"] = all(positive(v) is not None for v in
        (snapshot.structure.get("spot"), snapshot.structure.get("future"), snapshot.volatility.get("level")))
    if not quality["critical_values_present"]:
        issues.append("Critical values unavailable")
    coverage = snapshot.options.get("coverage") or {}
    expected, received = number(coverage.get("expected_contracts")), number(coverage.get("received_contracts"))
    percent = 100*received/expected if expected is not None and expected > 0 and received is not None and 0 <= received <= expected else None
    quality["option_coverage_percent"] = percent
    if percent is None or percent < config.minimum_option_coverage:
        issues.append("Insufficient options-chain coverage")
    breadth_coverage = number(snapshot.breadth.get("coverage_percent"))
    quality["breadth_coverage_percent"] = breadth_coverage
    if (breadth_coverage is None or not config.minimum_breadth_coverage <= breadth_coverage <= 100
            or snapshot.breadth.get("full_index") is not True):
        issues.append("Insufficient full-index breadth coverage")
    if snapshot.options.get("index", snapshot.index_name) != snapshot.index_name or snapshot.breadth.get("index", snapshot.index_name) != snapshot.index_name:
        issues.append("Snapshot index mismatch")
    if len(aligned) < config.minimum_aligned:
        issues.append("Fewer than minimum aligned categories")
    if max(bull, bear) < config.minimum_score:
        issues.append("Winning score below minimum")
    if abs(bull-bear) < config.minimum_separation or winner == "neutral":
        issues.append("Directional scores too close")
    contradictions = [f"{name}: {item}" for name, c in scores.categories.items() for item in c.contradictions]
    for name in ("price_trend", "options_positioning", "futures_structure", "breadth_constituents"):
        category = scores.categories[name]
        opposing = category.bearish_points if winner == "bullish" else category.bullish_points
        if ((name == "futures_structure" and "Spot and futures move in opposing directions" in category.contradictions) or
                category.direction not in (winner, "neutral", "unavailable") or
                (category.available_weight > 0 and opposing >= category.available_weight*config.contradiction_fraction)):
            reason = "Major category contradiction: " + name
            issues.append(reason)
            contradictions.append(reason)
    quality["blocking_reasons"] = tuple(issues)
    quality["score_version"] = "5.3.1"
    quality["config"] = asdict(config)
    # Confidence is evidence strength on the original 100-point budget, not a
    # calibrated probability. Blocked decisions deliberately report zero.
    return DecisionResult("NO_TRADE" if issues else "CALL" if winner == "bullish" else "PUT",
                          0 if issues else round(max(bull, bear), 6), bull, bear, aligned, scores.categories,
                          tuple(f"{name}: {item}" for name, c in scores.categories.items() for item in c.evidence),
                          tuple(contradictions), quality)
