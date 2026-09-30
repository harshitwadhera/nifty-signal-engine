from datetime import datetime, time

from app.analytics.session import IST
from app.options.discovery import atm
from app.options.pricing import implied_greeks
from app.options.quality import fresh_price, field_available, positioning_usable, paired_rows


def positioning(row, config):
    price, oi = row["price_change_percent"], row["oi_change_percent"]
    if price is None or oi is None:
        return "UNAVAILABLE"
    if abs(price) <= config.price_change_percent or abs(oi) <= config.oi_change_percent:
        return "NEUTRAL"
    return ("LONG_BUILDUP" if price > 0 else "SHORT_BUILDUP") if oi > 0 else ("SHORT_COVERING" if price > 0 else "LONG_UNWINDING")


def liquidity(row, config):
    bid, ask = row.get("bid"), row.get("ask")
    spread = ask-bid if bid is not None and ask is not None and 0 < bid <= ask else None
    percent = 100*spread/((bid+ask)/2) if spread is not None else None
    state = "POOR"
    if percent is not None and (row.get("bid_quantity") or 0) > 0 and (row.get("ask_quantity") or 0) > 0:
        oi, volume = row.get("oi") or 0, row.get("volume") or 0
        if percent <= config.liquid_spread_percent and oi >= config.liquid_min_oi and volume >= config.liquid_min_volume:
            state = "LIQUID"
        elif percent <= config.moderate_spread_percent and oi >= config.moderate_min_oi and volume >= config.moderate_min_volume:
            state = "MODERATE"
    return {"spread": spread, "spread_percent": percent, "liquidity_state": state}


def pcr(rows, field):
    if not rows or any(r.get(field) is None for r in rows) or not all(any(r["option_type"] == k for r in rows) for k in ("CE", "PE")):
        return None
    call = sum(r[field] for r in rows if r["option_type"] == "CE")
    put = sum(r[field] for r in rows if r["option_type"] == "PE")
    return put/call if call > 0 else None


def walls(rows, flow_rows=None):
    flow_rows = rows if flow_rows is None else flow_rows
    result = {}
    for kind, name in (("CE", "call"), ("PE", "put")):
        side = [r for r in rows if r["option_type"] == kind]
        def rank(field, predicate, reverse=True):
            eligible = [r for r in side if r.get(field) is not None and predicate(r[field])]
            return [{"strike": r["strike"], "value": r[field]} for r in sorted(eligible, key=lambda r: r[field], reverse=reverse)[:3]]
        top = rank("oi", lambda value: value > 0)
        side = [r for r in flow_rows if r["option_type"] == kind]
        additions = rank("oi_change_session", lambda value: value > 0)
        writing = [r for r in side if r.get("positioning") == "SHORT_BUILDUP"]
        side = [r for r in side if r.get("positioning") in ("SHORT_COVERING", "LONG_UNWINDING")]
        unwinding = rank("oi_change_session", lambda value: value < 0, False)
        result.update({name+"_oi_wall": top[0]["strike"] if top else None,
                       "top_3_"+name+"_oi": top, name+"_oi_additions": additions,
                       name+"_unwinding_zones": unwinding,
                       name+"_writing_zones": [{"strike": r["strike"], "oi_change_session": r["oi_change_session"]}
                             for r in sorted(writing, key=lambda r: r["oi_change_session"], reverse=True)[:3]]})
        result["highest_"+name+"_oi_addition"] = additions[0] if additions else None
        result["largest_"+name+"_unwinding"] = unwinding[0] if unwinding else None
    return result


def max_pain(rows):
    if not rows or {r["option_type"] for r in rows} != {"CE", "PE"} or any(r.get("oi") is None for r in rows) or sum(r["oi"] for r in rows) <= 0:
        return None
    strikes = sorted({r["strike"] for r in rows})
    # O(n log n) prefix sums: no quadratic iteration over large chains.
    calls = {s: 0 for s in strikes}
    puts = calls.copy()
    for r in rows:
        (calls if r["option_type"] == "CE" else puts)[r["strike"]] += r["oi"]
    call_q = call_k = 0
    put_q, put_k = sum(puts.values()), sum(k*v for k,v in puts.items())
    best = None
    for strike in strikes:
        put_q -= puts[strike]
        put_k -= strike*puts[strike]
        cost = strike*call_q-call_k+put_k-strike*put_q
        if best is None or cost < best[0]:
            best = cost, strike
        call_q += calls[strike]
        call_k += strike*calls[strike]
    return best[1]


def enrich(row, spot, future, now, config, fresh=True):
    row = {**row, **liquidity(row, config)}
    row["positioning"] = positioning(row, config) if fresh else "UNAVAILABLE"
    expiry = datetime.combine(datetime.fromisoformat(row["expiry"]).date(), time(15, 30), IST)
    years = (expiry-now).total_seconds()/(365*86400)
    model = "black76" if future is not None else "black_scholes_q0"
    price = (row["bid"]+row["ask"])/2 if row["spread"] is not None else row["ltp"]
    row.update(implied_greeks(price if fresh else None, future if future is not None else spot, row["strike"], years, config.risk_free_rate, row["option_type"], model))
    row.update(greeks_model=model, greeks_source="locally_calculated", greeks_underlying="expiry_matched_future" if future is not None else "spot_zero_dividend_assumption",
               greeks_price_source="bid_ask_mid" if row["spread"] is not None else "ltp", stale=not fresh)
    return row


class OptionsAnalytics:
    @staticmethod
    def summarize(index, expiry, rows, spot, future, expected, now, refresh, last_tick, config, window=10):
        valid = [r for r in rows if fresh_price(r)]
        complete = len(valid) == expected and expected > 0
        oi_rows = [r for r in rows if field_available(r, "oi")]
        flow_rows = [r for r in rows if positioning_usable(r)]
        oi_complete = complete and len(oi_rows) == expected
        center = atm(sorted({r["strike"] for r in rows}), spot)
        strikes = sorted({r["strike"] for r in rows})
        position = strikes.index(center) if center is not None else 0
        near_strikes = set(strikes[max(0, position-window):position+window+1]) if center is not None else set()
        near = [r for r in rows if r["strike"] in near_strikes]
        near_valid = [r for r in near if fresh_price(r)]
        levels = walls(oi_rows, flow_rows)
        pain = max_pain(rows) if oi_complete else None
        ratios, ratio_quality = {}, {}
        for key, population, field in (("pcr_oi", rows, "oi"), ("pcr_volume", rows, "volume"),
                                       ("near_atm_pcr_oi", near, "oi"), ("near_atm_pcr_volume", near, "volume")):
            usable = paired_rows(population, field)
            ratios[key] = pcr(usable, field)
            ratio_quality[key] = {"usable_contracts": len(usable), "paired_strikes": len(usable)//2,
                                  "available": ratios[key] is not None,
                                  "reason": "Matched fresh CE/PE strikes" if ratios[key] is not None
                                  else "Matched fresh CE/PE data and positive call denominator required"}
        def coverage(population, fresh):
            return {"expected_contracts": population, "fresh_contracts": fresh,
                    "percent": round(100*fresh/population, 1) if population else 0}
        return {"index": index, "selected_expiry": expiry.isoformat() if expiry else None, "spot": spot, "futures": future,
                "atm": center, **ratios, "pcr_scope": "matched_fresh_strikes_selected_expiry", "pcr_quality": ratio_quality,
                "near_atm_window": window, "near_atm_quality": coverage(len(near), len(near_valid)),
                "max_pain": pain, "distance_from_spot": pain-spot if pain is not None and spot is not None else None,
                **levels, "oi_change_basis": "session_first_observation",
                "positioning_quality": {side: sum(r["option_type"] == kind for r in flow_rows)
                                        for side, kind in (("call", "CE"), ("put", "PE"))},
                "oi_quality": {side: sum(r["option_type"] == kind and r["oi"] > 0 for r in oi_rows)
                               for side, kind in (("call", "CE"), ("put", "PE"))},
                "atm_ce": next((r for r in rows if r["strike"] == center and r["option_type"] == "CE"), None),
                "atm_pe": next((r for r in rows if r["strike"] == center and r["option_type"] == "PE"), None),
                "timestamp": now.isoformat(), "last_full_chain_refresh_at": refresh,
                # evidence_fresh describes the derived observations, never total coverage.
                # Legacy stale remains a full-chain diagnostic for existing API consumers.
                "evidence_fresh": True, "last_stream_tick_at": last_tick,
                "full_chain_fresh": complete, "stale": not complete,
                "coverage": {**coverage(expected, len(valid)), "received_contracts": len(valid),
                             "non_fresh_contracts": max(0, expected-len(valid)),
                             "oi_contracts": len(oi_rows),
                             "oi_change_contracts": sum(field_available(r, "oi_change_session") for r in rows),
                             "volume_contracts": sum(field_available(r, "volume") for r in rows)}}
