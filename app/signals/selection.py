"""Select only an existing ATM or adjacent ITM contract, never by cheap premium."""
from datetime import datetime, time

from app.analytics.session import IST
from .engine import positive, number
from .execution_models import SelectedOption, instant


def liquid(row, now, config):
    if row.get('stale') is not False or row.get('liquidity_state') not in ('LIQUID', 'MODERATE'):
        return False
    try:
        received = instant(row['received_at'])
        # Exchange time is checked if supplied; receipt time is the fallback.
        quoted = instant(row.get('timestamp') or row['received_at'])
        if not all(0 <= (now-t).total_seconds() <= config.quote_max_age_seconds for t in (received, quoted)):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    bid, ask = positive(row.get('bid')), positive(row.get('ask'))
    oi, volume = number(row.get('oi')), number(row.get('volume'))
    if bid is None or ask is None or ask < bid or positive(row.get('ltp')) is None:
        return False
    spread = 200*((ask-bid)/ask)/(1+bid/ask)
    return (spread <= config.max_spread_percent and positive(row.get('bid_quantity')) is not None
            and positive(row.get('ask_quantity')) is not None and oi is not None and oi >= config.minimum_oi
            and volume is not None and volume >= config.minimum_volume)


def select_option(snapshot, direction, rows, config):
    if direction not in ('CALL', 'PUT'):
        return None
    now = instant(snapshot.as_of)
    expiry = snapshot.options.get('selected_expiry')
    # Use only nearest currently selected expiry. No silent alternative expiry.
    if not expiry or snapshot.options.get('expiry_selection', {}).get('nearest') != expiry:
        return None
    try:
        if datetime.combine(datetime.fromisoformat(expiry).date(), time(15, 30), IST) <= now:
            return None
    except (ValueError, TypeError):
        return None
    contracts = [r for r in rows if r.get('index') == snapshot.index_name and r.get('expiry') == expiry
                 and positive(r.get('strike')) is not None and r.get('option_type') in ('CE', 'PE')]
    strikes = sorted({r['strike'] for r in contracts})
    spot = positive(snapshot.structure.get('spot'))
    if not strikes or spot is None:
        return None
    atm = min(strikes, key=lambda k: (abs(k-spot), k))
    # A complete metadata chain is required to know that adjacent really means
    # one step; callers pass the unfiltered Phase 4 chain, not API pagination.
    expected = number(snapshot.options.get('coverage', {}).get('expected_contracts'))
    identities = {(r['strike'], r['option_type']) for r in contracts}
    if expected is None or expected != len(contracts) or len(identities) != len(contracts):
        return None
    if (len({r.get('instrument_token') for r in contracts}) != len(contracts)
            or len({r.get('trading_symbol') for r in contracts}) != len(contracts)):
        return None
    kind = 'CE' if direction == 'CALL' else 'PE'
    index = strikes.index(atm)
    adjacent = index-1 if direction == 'CALL' else index+1
    candidates = [atm]
    if 0 <= adjacent < len(strikes):
        candidates.append(strikes[adjacent])
    for strike in candidates:
        matches = [r for r in contracts if r['strike'] == strike and r['option_type'] == kind]
        if len(matches) != 1:
            continue
        row = matches[0]
        token = row.get('instrument_token')
        if (not isinstance(token, int) or isinstance(token, bool) or token <= 0
                or not isinstance(row.get('trading_symbol'), str) or not row['trading_symbol'] or not liquid(row, now, config)):
            continue
        return SelectedOption(row['trading_symbol'], token, expiry, strike, kind,
                              row.get('timestamp') or row['received_at'], row['bid'], row['ask'],
                              200*((row['ask']-row['bid'])/row['ask'])/(1+row['bid']/row['ask']), row['oi'], row['volume'])
    return None
