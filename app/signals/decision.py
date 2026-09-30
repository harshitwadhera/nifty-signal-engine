"""Fail-closed decision from one supplied snapshot; no entry/lifecycle logic."""
from datetime import datetime
from dataclasses import asdict

from .engine import number, positive
from app.options.state import finite_json
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


def gate(key, label, passed, actual=None, required=None, detail=None):
    return dict(key=key, label=label, passed=passed,
                status="INFO" if passed is None else "PASS" if passed else "BLOCK",
                actual=actual, required=required, detail=detail)


def decide(snapshot, scores, config):
    bull, bear = scores.bullish_points, scores.bearish_points
    winner = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    aligned = tuple(k for k, c in scores.categories.items() if c.direction == winner and winner != "neutral")
    issues = []
    gates = []
    def check(key, label, passed, actual, required, reason, detail=None):
        gates.append(gate(key, label, passed, actual, required, detail))
        if not passed:
            issues.append(reason)
    quality = {name: fresh(source, timestamp, snapshot.as_of, config.critical_max_age_seconds)
               for name, source, timestamp in (
                   ("structure_fresh", snapshot.structure, "as_of"),
                   ("breadth_fresh", snapshot.breadth, "as_of"),
                   ("vix_fresh", snapshot.volatility, "as_of"))}
    option_score = scores.categories["options_positioning"]
    # The summary timestamp describes row-level validation at observation time.
    # REST completion time and full-chain stale are diagnostic, not freshness gates.
    quality["options_signal_data"] = (option_score.available_weight > 0 and fresh(
        {"stale": False, "timestamp": snapshot.options.get("timestamp")}, "timestamp",
        snapshot.as_of, config.critical_max_age_seconds))
    quality["options_quality"] = {key: snapshot.options.get(key) for key in
        ("selected_expiry", "coverage", "full_chain_fresh", "near_atm_quality", "pcr_quality", "positioning_quality", "oi_quality")}
    quality["options_quality"].update(component_availability=option_score.component_availability,
        atm_quality=option_score.component_availability.get("atm_behavior", {}).get("sides", {}),
        options_total_available_weight=option_score.available_weight)
    quality["options_quality"] = finite_json(quality["options_quality"])
    for key, label in (("structure_fresh", "Structure freshness"), ("options_signal_data", "Options signal data"),
                       ("breadth_fresh", "Breadth freshness"), ("vix_fresh", "VIX freshness")):
        detail = (("Usable component evidence" if quality[key] else "No current usable component evidence")
                  if key == "options_signal_data" else "Fresh" if quality[key] else "Stale or freshness unavailable")
        gates.append(gate(key, label, quality[key], "fresh" if quality[key] else "stale or unavailable",
                          f"Fresh within {config.critical_max_age_seconds:g}s", detail))
    if not all(quality[k] for k in ("structure_fresh", "options_signal_data", "breadth_fresh", "vix_fresh")):
        issues.append("Critical data stale or freshness unavailable")
    quality["critical_values_present"] = all(positive(v) is not None for v in
        (snapshot.structure.get("spot"), snapshot.structure.get("future"), snapshot.volatility.get("level")))
    check("critical_values_present", "Critical values", quality["critical_values_present"],
          quality["critical_values_present"], True, "Critical values unavailable", "Spot, futures and VIX must be positive finite values")
    coverage = snapshot.options.get("coverage") or {}
    expected, received = number(coverage.get("expected_contracts")), number(coverage.get("fresh_contracts", coverage.get("received_contracts")))
    percent = 100*received/expected if expected is not None and expected > 0 and received is not None and 0 <= received <= expected else None
    quality["option_coverage_percent"] = percent
    gates.append(gate("option_coverage", "Overall options chain coverage (%)", None, percent,
                      detail=f"{received:g} / {expected:g} fresh contracts; diagnostic only" if percent is not None
                      else "Coverage unavailable; diagnostic only"))
    gates.append(gate("full_chain_fresh", "Full-chain freshness", None, snapshot.options.get("full_chain_fresh"),
                      detail="Diagnostic only; max pain may be unavailable"))
    for side, detail in quality["options_quality"]["atm_quality"].items():
        gates.append(gate("atm_"+side, "ATM "+side.upper()+" data", True if detail["usable"] else None,
                          detail["liquidity"], detail=detail["reason"]))
    breadth_coverage = number(snapshot.breadth.get("coverage_percent"))
    quality["breadth_coverage_percent"] = breadth_coverage
    check("breadth_coverage", "Breadth coverage (%)",
          breadth_coverage is not None and config.minimum_breadth_coverage <= breadth_coverage <= 100
          and snapshot.breadth.get("full_index") is True, breadth_coverage, config.minimum_breadth_coverage,
          "Insufficient full-index breadth coverage", "Full-index coverage required; " +
          ("full index available" if snapshot.breadth.get("full_index") is True else "full index unavailable"))
    check("index_match", "Snapshot index", snapshot.options.get("index", snapshot.index_name) == snapshot.index_name
          and snapshot.breadth.get("index", snapshot.index_name) == snapshot.index_name,
          None, snapshot.index_name, "Snapshot index mismatch")
    check("minimum_aligned", "Aligned categories", len(aligned) >= config.minimum_aligned,
          len(aligned), config.minimum_aligned, "Fewer than minimum aligned categories")
    check("minimum_score", "Winning score", max(bull, bear) >= config.minimum_score,
          max(bull, bear), config.minimum_score, "Winning score below minimum")
    check("minimum_separation", "Score separation", abs(bull-bear) >= config.minimum_separation and winner != "neutral",
          abs(bull-bear), config.minimum_separation, "Directional scores too close")
    contradictions = [f"{name}: {item}" for name, c in scores.categories.items() for item in c.contradictions]
    major = []
    for name in ("price_trend", "options_positioning", "futures_structure", "breadth_constituents"):
        category = scores.categories[name]
        opposing = category.bearish_points if winner == "bullish" else category.bullish_points
        if ((name == "futures_structure" and "Spot and futures move in opposing directions" in category.contradictions) or
                category.direction not in (winner, "neutral", "unavailable") or
                (category.available_weight > 0 and opposing >= category.available_weight*config.contradiction_fraction)):
            reason = "Major category contradiction: " + name
            issues.append(reason)
            contradictions.append(reason)
            major.append(name)
    gates.append(gate("major_contradiction", "Major contradictions", not major,
                      ", ".join(major) if major else "None", "None"))
    selection = snapshot.options.get("expiry_selection") or {}
    if selection:
        check("analysis_expiry", "Analysis expiry consistency", bool(selection.get("analysis_expiry"))
              and selection["analysis_expiry"] == snapshot.options.get("selected_expiry"),
              snapshot.options.get("selected_expiry"), selection.get("analysis_expiry"),
              "Analysis expiry unavailable or differs from scoring expiry")
    quality["blocking_reasons"] = tuple(issues)
    quality["score_version"] = "5.6.0"
    quality["config"] = asdict(config)
    # Confidence is evidence strength on the original 100-point budget, not a
    # calibrated probability. Blocked decisions deliberately report zero.
    return DecisionResult("NO_TRADE" if issues else "CALL" if winner == "bullish" else "PUT",
                          0 if issues else round(max(bull, bear), 6), bull, bear, aligned, scores.categories,
                          tuple(f"{name}: {item}" for name, c in scores.categories.items() for item in c.evidence),
                          tuple(contradictions), quality, tuple(gates), selection)
