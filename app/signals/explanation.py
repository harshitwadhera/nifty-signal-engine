"""Finite, credential-free explanation capture at observation time."""
from dataclasses import asdict
from datetime import date, datetime, time
from math import isfinite


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()
                if not any(term in str(k).lower() for term in ('secret', 'password', 'authorization', 'request_token', 'access_token', 'api_key', 'url'))}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def explain(snapshot, contracts, planner, bar=None):
    decision = planner.engine.decide(snapshot)
    # Test seams may provide a minimal decision; production uses DecisionResult.
    scored = asdict(decision) if hasattr(type(decision), '__dataclass_fields__') else {'decision': decision.decision}
    strikes = sorted({r['strike'] for r in contracts if isinstance(r.get('strike'), (int, float)) and isfinite(r['strike'])})
    spot = snapshot.structure.get('spot')
    center = min(strikes, key=lambda s: abs(s-spot)) if strikes and isinstance(spot, (int, float)) and isfinite(spot) else None
    near = set(strikes[max(0, strikes.index(center)-2):strikes.index(center)+3]) if center is not None else set()
    walls = {snapshot.options.get('call_oi_wall'), snapshot.options.get('put_oi_wall')}
    fields = ('index', 'expiry', 'strike', 'option_type', 'instrument_token', 'trading_symbol', 'ltp',
              'bid', 'ask', 'bid_quantity', 'ask_quantity', 'spread', 'spread_percent', 'oi', 'volume',
              'oi_change_session', 'oi_change_vs_previous_close', 'volume_change', 'iv', 'delta', 'gamma',
              'theta', 'vega', 'liquidity_state', 'positioning', 'timestamp', 'received_at', 'source', 'stale')
    context = [{k: r.get(k) for k in fields} for r in contracts if r.get('strike') in near | walls]
    return safe({'version': '5.5.1', 'as_of': snapshot.as_of, 'index_name': snapshot.index_name,
                 'scores': scored, 'input': asdict(snapshot), 'option_chain_context': context,
                 'chain_context_scope': 'ATM +/-2 strikes and OI walls; scoring uses captured full-chain summary',
                 'execution_config': asdict(planner.config),
                 'scoring_config': asdict(planner.engine.config) if hasattr(type(planner.engine.config), '__dataclass_fields__') else {},
                 'confirmation_bar': bar})
