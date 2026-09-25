"""Observed underlying excursions and model outcomes, not fills or option P&L."""
from .execution_models import instant


def entered(price, option_ltp, now):
    return dict(entry_time=now.isoformat(), entry_underlying=price, entry_option_ltp=option_ltp,
                t1_hit=False, t1_time=None, t2_hit=False, t2_time=None, stop_hit=False, stop_time=None,
                mfe=0.0, mae=0.0, mfe_r=0.0, mae_r=0.0, result_r=None, duration_seconds=0,
                time_to_t1_seconds=None, time_to_t2_seconds=None, time_to_stop_seconds=None,
                last_price=price, last_observed_at=now.isoformat(), observation_gap=False,
                basis='sampled underlying observations; not broker fills or option returns')


def observe(record, price, favorable, adverse, now, stop_touched=False):
    result = dict(record.outcome)
    if not result:
        return result  # Legacy record: never invent an entry or backfill evidence.
    entry = result['entry_underlying']
    sign = 1 if record.plan.direction == 'CALL' else -1
    risk = abs(entry-record.plan.invalidation.level)
    # Stop/target ordering inside a completed bar is unknown. Do not credit the
    # favorable extreme from a bar that also stopped the signal.
    favorable = entry if stop_touched else favorable
    result['mfe'] = max(result['mfe'], sign*(favorable-entry), 0)
    result['mae'] = max(result['mae'], sign*(entry-adverse), 0)
    result['mfe_r'], result['mae_r'] = result['mfe']/risk, result['mae']/risk
    result['duration_seconds'] = (now-instant(result['entry_time'])).total_seconds()
    result['observation_gap'] |= (now-instant(result['last_observed_at'])).total_seconds() > 30
    result.update(last_price=price, last_observed_at=now.isoformat())
    return result


def transitioned(record, state, now):
    result = dict(record.outcome)
    if not result:
        return result
    duration = (now-instant(result['entry_time'])).total_seconds()
    result['duration_seconds'] = duration
    for target, event in (('t1', 'TARGET1_HIT'), ('t2', 'TARGET2_HIT'), ('stop', 'STOPPED')):
        if state == event:
            result[target+'_hit'] = True
            result[target+'_time'] = now.isoformat()
            result['time_to_'+target+'_seconds'] = duration
    sign = 1 if record.plan.direction == 'CALL' else -1
    risk = abs(result['entry_underlying']-record.plan.invalidation.level)
    exit_price = None
    if state == 'STOPPED':
        exit_price = min(record.plan.invalidation.level, result['last_price']) if sign == 1 else max(record.plan.invalidation.level, result['last_price'])
    elif state == 'TARGET2_HIT':
        exit_price = record.plan.target2.level
    elif state == 'EXPIRED':
        result['observation_gap'] |= (now-instant(result['last_observed_at'])).total_seconds() > 30
        # Expiry with no fresh underlying observation cannot imply an exit price.
        if not result['observation_gap'] and 0 <= (now-instant(result['last_observed_at'])).total_seconds() <= 30:
            exit_price = result['last_price']
    if exit_price is not None:
        result['result_r'] = sign*(exit_price-result['entry_underlying'])/risk
    if state in ('STOPPED', 'TARGET2_HIT', 'EXPIRED'):
        result['exit_time'] = now.isoformat()
        result['exit_underlying'] = exit_price
    return result
