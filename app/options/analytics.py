from datetime import datetime, time

from app.analytics.session import IST
from app.options.discovery import atm
from app.options.pricing import implied_greeks


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


def walls(rows):
    result = {}
    for kind, name in (("CE", "call"), ("PE", "put")):
        side = [r for r in rows if r["option_type"] == kind]
        def rank(field, predicate, reverse=True):
            eligible = [r for r in side if r.get(field) is not None and predicate(r[field])]
            return [{"strike": r["strike"], "value": r[field]} for r in sorted(eligible, key=lambda r: r[field], reverse=reverse)[:3]]
        top = rank("oi", lambda value: value > 0)
        additions = rank("oi_change_session", lambda value: value > 0)
        unwinding = rank("oi_change_session", lambda value: value < 0, False)
        writing = [r for r in side if r.get("positioning") == "SHORT_BUILDUP"]
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
        valid = [r for r in rows if r["ltp"] is not None and not r["stale"]]
        complete = len(valid) == expected and expected > 0
        oi_complete = complete and {r["option_type"] for r in rows} == {"CE", "PE"} and all(r["oi"] is not None for r in rows)
        volume_complete = complete and all(r["volume"] is not None for r in rows)
        center = atm(sorted({r["strike"] for r in rows}), spot)
        strikes = sorted({r["strike"] for r in rows})
        position = strikes.index(center) if center is not None else 0
        near_strikes = set(strikes[max(0, position-window):position+window+1]) if center is not None else set()
        near = [r for r in rows if r["strike"] in near_strikes]
        near_fresh = bool(near) and all(not r["stale"] and r["ltp"] is not None for r in near)
        levels = walls(rows if oi_complete else [])
        change_complete = oi_complete and all(r.get("oi_change_session") is not None for r in rows)
        if not change_complete:
            for name in ("call", "put"):
                for key in ("oi_additions", "unwinding_zones", "writing_zones"):
                    levels[name+"_"+key] = []
                levels["highest_"+name+"_oi_addition"] = levels["largest_"+name+"_unwinding"] = None
        pain = max_pain(rows) if oi_complete else None
        return {"index": index, "selected_expiry": expiry.isoformat() if expiry else None, "spot": spot, "futures": future,
                "atm": center, "pcr_oi": pcr(rows, "oi") if oi_complete else None,
                "pcr_volume": pcr(rows, "volume") if volume_complete else None, "pcr_scope": "full_selected_expiry",
                "near_atm_pcr_oi": pcr(near, "oi") if near_fresh else None,
                "near_atm_pcr_volume": pcr(near, "volume") if near_fresh else None,
                "near_atm_window": window, "max_pain": pain, "distance_from_spot": pain-spot if pain is not None and spot is not None else None,
                **levels, "oi_change_basis": "session_first_observation",
                "atm_ce": next((r for r in rows if r["strike"] == center and r["option_type"] == "CE"), None),
                "atm_pe": next((r for r in rows if r["strike"] == center and r["option_type"] == "PE"), None),
                "timestamp": now.isoformat(), "last_full_chain_refresh_at": refresh,
                "last_stream_tick_at": last_tick, "stale": not complete,
                "coverage": {"expected_contracts": expected, "received_contracts": len(valid),
                             "percent": round(100*len(valid)/expected, 1) if expected else 0,
                             "oi_contracts": sum(r["oi"] is not None and not r["stale"] for r in rows),
                             "oi_change_contracts": sum(r.get("oi_change_session") is not None and not r["stale"] for r in rows),
                             "volume_contracts": sum(r["volume"] is not None and not r["stale"] for r in rows)}}
