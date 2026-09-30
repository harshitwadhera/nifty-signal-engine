"""Row-level eligibility shared by analytics and scoring. No chain thresholds."""
from app.options.state import numeric


BEHAVIOR = {"LONG_BUILDUP": 1, "SHORT_COVERING": 1, "SHORT_BUILDUP": -1,
            "LONG_UNWINDING": -1, "NEUTRAL": 0}


def positive(value):
    return numeric(value) and value > 0


def fresh_price(row):
    return row.get("stale") is False and positive(row.get("ltp"))


def field_available(row, field):
    value = row.get(field)
    return row.get("stale") is False and numeric(value) and (field == "oi_change_session" or value >= 0)


def positioning_usable(row):
    return (fresh_price(row) and field_available(row, "oi") and field_available(row, "oi_change_session")
            and numeric(row.get("price_change_percent")) and numeric(row.get("oi_change_percent"))
            and row.get("positioning") in BEHAVIOR)


def atm_quality(row, max_iv):
    row = row or {}
    checks = ((fresh_price(row), "Fresh positive LTP required"),
              (row.get("liquidity_state") in ("LIQUID", "MODERATE"), "Liquid or moderate liquidity required"),
              (positive(row.get("bid")) and positive(row.get("ask")) and row["ask"] >= row["bid"]
               and positive(row.get("bid_quantity")) and positive(row.get("ask_quantity")), "Valid bid/ask depth required"),
              (positive(row.get("iv")) and row["iv"] <= max_iv, "Valid IV required"),
              (positioning_usable(row), "Price/OI positioning unavailable"))
    reasons = [reason for passed, reason in checks if not passed]
    return {"fresh": fresh_price(row), "liquidity": row.get("liquidity_state", "UNAVAILABLE"),
            "usable": not reasons, "reason": "; ".join(reasons) or "Fresh, liquid, valid price/OI behavior"}


def paired_rows(rows, field):
    """Use the same strike population on each side of a PCR ratio."""
    eligible = [r for r in rows if field_available(r, field)]
    strikes = {r["strike"] for r in eligible if r["option_type"] == "CE"} & {
        r["strike"] for r in eligible if r["option_type"] == "PE"}
    return [r for r in eligible if r["strike"] in strikes]
