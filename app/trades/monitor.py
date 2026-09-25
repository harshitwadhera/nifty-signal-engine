"""Pure, deterministic transition function shared by live monitoring and replay."""
from copy import deepcopy
from math import isfinite
from app.signals.execution_models import instant

SYMBOLS = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK'}
FRESH_SECONDS = 30


def price(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0 else None


def fresh(row, now, connected=True):
    try:
        stamps = [row['last_tick_received_at']]
        if row.get('last_exchange_timestamp'):
            stamps.append(row['last_exchange_timestamp'])
        return (connected and row.get('stale') is False and price(row.get('last_price')) is not None
                and all(0 <= (instant(now)-instant(t)).total_seconds() < FRESH_SECONDS for t in stamps))
    except (KeyError, ValueError, TypeError):
        return False


def observe(trade, row, connected, now):
    result = deepcopy(trade)
    if result['status'] == 'USER_CLOSED':
        return result, []
    valid = row.get('trading_symbol') == SYMBOLS[trade['index_name']] and fresh(row, now, connected)
    result['monitoring_status'] = 'LIVE' if valid else 'PAUSED'
    if not valid:
        if trade.get('monitoring_status') != 'PAUSED':
            result['metadata']['data_gap_count'] = trade['metadata'].get('data_gap_count', 0)+1
            result['metadata']['last_gap_at'] = instant(now).isoformat()
        return result, []
    stamp = instant(row['last_tick_received_at'])
    watermark = trade['metadata'].get('last_observation_at', trade['opened_at'])
    if stamp <= instant(watermark):
        return result, []
    value = row['last_price']
    result['metadata'].update(last_observation_at=stamp.isoformat(), current_underlying=value)
    sign = 1 if trade['direction'] == 'CALL' else -1
    kinds = []
    # A stop event remains latched until the user closes; targets never close a trade.
    if trade['status'] != 'STOP_HIT':
        if sign*(value-trade['underlying_stop']) <= 0:
            kinds = ['STOP_HIT']
        else:
            for key, kind in (('target1', 'T1_HIT'), ('target2', 'T2_HIT')):
                if trade.get(key) is not None and sign*(value-trade[key]) >= 0 and kind not in trade['metadata']['hits']:
                    kinds.append(kind)
    events = []
    for kind in kinds:
        if kind in result['metadata']['hits']:
            continue
        result['metadata']['hits'].append(kind)
        result['status'] = kind
        events.append({'event_id': trade['trade_id']+':'+kind, 'trade_id': trade['trade_id'],
                       'kind': kind, 'at': stamp.isoformat(), 'underlying': value, 'acknowledged_at': None})
    return result, events
