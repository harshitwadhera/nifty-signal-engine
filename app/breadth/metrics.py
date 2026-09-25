from app.signals.engine import positive


def calculate(index, membership, observations, as_of):
    """Pure breadth over supplied observations. Denominators never hide gaps."""
    symbols = membership["symbols"]
    supplied = membership.get("weights", {})
    weighted = bool(symbols) and set(supplied) == set(symbols) and all(positive(v) for v in supplied.values())
    weights = supplied if weighted else {s: 1 for s in symbols}
    # Normalize first to avoid overflow for valid but large configured weights.
    scale = max(weights.values(), default=1)
    weights = {s: v/scale for s, v in weights.items()}
    total = sum(weights.values())
    valid = {}
    for symbol in symbols:
        row = observations.get(symbol, {})
        price, close = positive(row.get("last_price")), positive(row.get("previous_close"))
        if not row.get("stale", True) and price is not None and close is not None:
            valid[symbol] = row
    advances = sum(r["last_price"] > r["previous_close"] for r in valid.values())
    declines = sum(r["last_price"] < r["previous_close"] for r in valid.values())
    observed = sum(weights[s] for s in valid)
    def percent(predicate, rows=valid):
        denominator = sum(weights[s] for s in rows)
        return 100*sum(weights[s] for s, r in rows.items() if predicate(r))/denominator if denominator else None
    result = {"index": index, "as_of": as_of, "full_index": membership.get("full_index", False),
              "membership_source": membership.get("source"), "membership_as_of": membership.get("as_of"),
              "weighting": "weighted" if weighted else "unweighted", "expected_constituents": len(symbols),
              "received_constituents": len(valid), "advances": advances, "declines": declines,
              "unchanged": len(valid)-advances-declines, "advance_decline_ratio": advances/declines if declines else None,
              "percent_positive": percent(lambda r: r["last_price"] > r["previous_close"]),
              "percent_negative": percent(lambda r: r["last_price"] < r["previous_close"]),
              "coverage_percent": 100*observed/total if total else 0,
              "count_coverage_percent": 100*len(valid)/len(symbols) if symbols else 0,
              "stale": not valid or not membership.get("membership_valid", False)}
    for interval in ("5m", "15m"):
        field = f"percent_above_{interval}_ema20"
        rows = {s: r for s, r in valid.items() if positive(r.get(interval+"_ema20")) is not None}
        result[field] = percent(lambda r: r["last_price"] > r[interval+"_ema20"], rows)
        result[field+"_coverage"] = 100*sum(weights[s] for s in rows)/total if total else 0
    return result
