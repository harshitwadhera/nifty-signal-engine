"""Past-only confirmed swing extraction from existing completed 5m bars."""
from datetime import timedelta

from .engine import positive
from .execution_models import instant


def completed_bars(rows, as_of, symbol):
    now = instant(as_of)
    result = []
    for row in rows:
        if row.get('symbol') != symbol or row.get('interval') != '5m' or row.get('completed') is not True or row.get('partial') is not False:
            continue
        try:
            start, end = instant(row['start_time']), instant(row['end_time'])
        except (TypeError, ValueError, KeyError):
            continue
        if end > now or start.date() != now.date() or end-start != timedelta(minutes=5):
            continue
        opened, high, low, close = [positive(row.get(k)) for k in ('open', 'high', 'low', 'close')]
        if any(v is None for v in (opened, high, low, close)) or not low <= min(opened, close) <= max(opened, close) <= high:
            continue
        result.append(row)
    # Duplicate bars cannot count as separate confirmations.
    return sorted({r['start_time']: r for r in result}.values(), key=lambda r: instant(r['start_time']))


def confirmed_swings(rows, as_of, symbol):
    bars = completed_bars(rows, as_of, symbol)
    swings = []
    for i in range(2, len(bars)-2):
        window = bars[i-2:i+3]
        if any(instant(a['end_time']) != instant(b['start_time']) for a, b in zip(window, window[1:])):
            continue
        center = window[2]
        for side in ('high', 'low'):
            neighbours = [r[side] for j, r in enumerate(window) if j != 2]
            if (center[side] > max(neighbours) if side == 'high' else center[side] < min(neighbours)):
                swings.append({'side': side, 'level': center[side], 'confirmed_at': window[-1]['end_time']})
    result = {'swing_levels': swings}
    for side in ('high', 'low'):
        matching = [s for s in swings if s['side'] == side]
        if matching:
            result['recent_swing_'+side] = matching[-1]['level']
    return result
