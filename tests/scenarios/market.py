"""Explicit synthetic mutations of the preserved user sample, using production logic.

The sample is an options SUMMARY, not a 276-contract chain. Additional contracts,
structure, breadth and VIX are labelled synthetic test inputs, not recovered data.
No SDK client, application startup, threads, network or configured DB is used.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from app.analytics.indicators import momentum
from app.options.analytics import OptionsAnalytics, enrich
from app.options.config import OptionsConfig
from app.options.discovery import OptionContract
from app.options.service import OptionsService
from app.options.state import OptionBook, normalize_quote
from app.signals import SignalEngine, SignalInput

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'nifty_options_baseline.json'
NOW = datetime.fromisoformat('2026-09-28T10:27:18.717699+05:30')
OPTIONS_CONFIG = OptionsConfig()  # Defaults, deliberately not environment-based .load().


def baseline():
    """A new independent copy on every call; never alter the on-disk sample."""
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def quote_fresh(quote, now=NOW):
    # Exercise the actual cache freshness predicate without starting OptionsService.
    return OptionsService._fresh(SimpleNamespace(config=OPTIONS_CONFIG), quote, now)


def synthetic_chain(original, index, bullish, expected, received):
    """Synthetic contract universe, based on the two supplied ATM row shapes.

    Explicitly replace identities, prices/depth, OI, volume, changes and clocks.
    Production enrich/summarize derive Greeks, positioning, walls and coverage.
    Missing fresh contracts are at the far upper wing, keeping ATM fresh.
    """
    spot = original['spot'] if index == 'NIFTY' else 55000.0
    center = original['atm'] if index == 'NIFTY' else 55000.0
    future = spot + 50
    rows = []
    for i in range(expected // 2):
        strike = center + (i - expected // 4) * 50
        for kind in ('CE', 'PE'):
            row = deepcopy(original['atm_' + kind.lower()])
            long = (kind == 'CE') == bullish
            premium = max(spot - strike if kind == 'CE' else strike - spot, 0) + 100
            oi = 10000 if (kind == 'CE') == bullish else 30000
            wall = center + ((-50 if kind == 'CE' else -100) if bullish else (100 if kind == 'CE' else 50))
            if strike == wall:
                oi *= 4
            stamp = NOW if len(rows) < received else NOW - timedelta(seconds=61)
            row.update(index=index, strike=strike, option_type=kind, lot_size=65 if index == 'NIFTY' else 35,
                       instrument_token=90000000 + len(rows), trading_symbol=f'SIM_{index}_{int(strike)}_{kind}',
                       ltp=premium, bid=premium-.1, ask=premium+.1, bid_quantity=100, ask_quantity=100,
                       depth={'buy': [{'price': premium-.1, 'quantity': 100, 'orders': 1}],
                              'sell': [{'price': premium+.1, 'quantity': 100, 'orders': 1}]},
                       oi=oi, volume=1000 if (kind == 'CE') == bullish else 3000,
                       oi_session_baseline=oi-1000, baseline_at=(NOW-timedelta(minutes=5)).isoformat(),
                       previous_close_oi=None, previous_oi_session_date=None, oi_change_vs_previous_close=None,
                       oi_change_session=1000, oi_change_intraday=1000, oi_change_percent=2,
                       price_change=2 if long else -2, price_change_percent=2 if long else -2,
                       volume_change=100, timestamp=stamp.isoformat(), received_at=NOW.isoformat(), source='rest')
            rows.append(enrich(row, spot, future, NOW, OPTIONS_CONFIG, quote_fresh(row)))
    return rows, spot, future


@dataclass
class Scenario:
    name: str
    snapshot: SignalInput
    rows: list
    notes: tuple = ('Synthetic scenario overrides; original baseline is preserved.',)


# Values here are input conditions, never precomputed scores or decisions.
SPECS = {
    'baseline': {}, 'healthy': {}, 'qualified_call': {}, 'qualified_put': {'bullish': False},
    'nifty_99_percent': {'received': 274}, 'coverage_above_95': {'received': 264},
    'coverage_below_95': {'received': 261}, 'banknifty_low_coverage': {'index': 'BANKNIFTY', 'received': 272},
    'stale_atm_ce': {}, 'stale_atm_pe': {'bullish': False}, 'stale_structure': {}, 'stale_vix': {},
    'breadth_89_9': {}, 'breadth_90': {}, 'breadth_95': {}, 'breadth_not_full': {},
    'bearish_51': {'bullish': False}, 'score_exactly_70': {}, 'three_aligned': {}, 'four_aligned': {},
    'separation_below_15': {}, 'separation_exactly_15': {}, 'separation_above_15': {},
    'futures_contradiction': {}, 'disconnected': {}, 'missing_spot': {}, 'missing_future': {},
    'missing_vix': {}, 'partial_candles': {},
}


def _neutral_options(options, spot):
    for side in ('call', 'put'):
        options[side + '_writing_zones'] = []
        options[side + '_unwinding_zones'] = []
    for kind in ('ce', 'pe'):
        options['atm_' + kind]['positioning'] = 'NEUTRAL'
    options.update(call_oi_wall=spot+100, put_oi_wall=spot-100,
                   pcr_oi=1, pcr_volume=1, near_atm_pcr_oi=1)


def build(name):
    spec = SPECS[name]
    original = baseline()
    index, bullish = spec.get('index', 'NIFTY'), spec.get('bullish', True)
    expected = 276 if index == 'NIFTY' else 312
    rows, spot, future = synthetic_chain(original, index, bullish, expected, spec.get('received', expected))
    options = deepcopy(original)
    options.update(OptionsAnalytics.summarize(index, date.fromisoformat(original['selected_expiry']), rows,
                   spot, future, expected, NOW, NOW.isoformat(), NOW.isoformat(), OPTIONS_CONFIG))
    for row in rows:
        row['distance_from_atm'] = row['strike'] - options['atm']
    options.update(front_month_futures=future, snapshot_started_at=NOW.isoformat())
    reference = spot / (1.1 if bullish else .9)
    structure = dict(spot=spot, future=future, futures_vwap=reference, futures_basis=50,
                     opening_range_high=reference*1.05, opening_range_low=reference*.95,
                     previous_day_high=reference*1.06, previous_day_low=reference*.94,
                     previous_day_close=reference, day_high=reference*1.11, day_low=reference*.89,
                     spot_change_percent=1 if bullish else -1, future_change_percent=1 if bullish else -1,
                     stale=False, as_of=NOW.isoformat())
    for interval in ('5m', '15m', '30m'):
        structure[interval] = dict(ema9=spot, ema20=reference, partial=False, stale=False)
    breadth = dict(index=index, as_of=NOW.isoformat(), stale=False, full_index=True, coverage_percent=100,
                   percent_positive=80 if bullish else 20, percent_negative=20 if bullish else 80,
                   percent_above_5m_ema20=80 if bullish else 20, percent_above_15m_ema20=80 if bullish else 20)
    vix = dict(level=18, change_percent=0, stale=False, as_of=NOW.isoformat())
    notes = ['Synthetic full-chain rows plus structure/breadth/VIX; no real chain was supplied.']
    if name == 'baseline':
        options = original
        structure = {'spot': original['spot'], 'future': original['futures']}
        breadth, vix, rows = {}, {}, []
        notes = ['Original options response unchanged; unsupplied structure/breadth/VIX are unavailable.']
    elif name.startswith('stale_atm_'):
        key = 'atm_' + name[-2:]
        row = deepcopy(options[key])
        row['timestamp'] = (NOW-timedelta(seconds=61)).isoformat()
        options[key] = enrich(row, spot, future, NOW, OPTIONS_CONFIG, quote_fresh(row))
        rows = [options[key] if r['instrument_token'] == row['instrument_token'] else r for r in rows]
        notes.append('Isolates the ATM-row guard with an otherwise fresh summary; no fallback ATM price is substituted.')
    elif name == 'stale_structure':
        structure['stale'] = True
    elif name == 'stale_vix':
        vix['stale'] = True
    elif name.startswith('breadth_'):
        if name == 'breadth_not_full':
            breadth['full_index'] = False
        else:
            breadth['coverage_percent'] = {'breadth_89_9': 89.9, 'breadth_90': 90, 'breadth_95': 95}[name]
    elif name == 'bearish_51':
        _neutral_options(options, spot)
        structure['futures_vwap'] = future
        notes.append('Neutral options and neutral VWAP; price, breadth and matching futures moves provide evidence.')
    elif name == 'score_exactly_70':
        structure.update(opening_range_high=spot+100, opening_range_low=spot-100)
        for side in ('put', 'call'):
            options[side+'_writing_zones'] = options[side+'_unwinding_zones'] = []
        options['pcr_volume'] = 1
        notes.append('Summary-level neutral OR, no positioning zones and neutral volume PCR isolate the score boundary.')
    elif name == 'three_aligned':
        breadth.update(percent_positive=50, percent_negative=50, percent_above_5m_ema20=50, percent_above_15m_ema20=50)
    elif name.startswith('separation_'):
        _neutral_options(options, spot)
        breadth.update(percent_positive=50, percent_negative=50, percent_above_5m_ema20=50, percent_above_15m_ema20=50)
        structure.update(futures_vwap=future, future_change_percent=0, spot_change_percent=0,
                         previous_day_high=spot+100, previous_day_low=spot-100, day_high=spot+100, day_low=spot-100)
        for interval in ('5m', '15m', '30m'):
            structure[interval].update(ema9=spot, ema20=spot)
        if name != 'separation_below_15':
            structure.update(day_high=spot+1, day_low=spot-100)
        if name == 'separation_above_15':
            structure['5m']['ema20'] = reference
        notes.append('Natural 12/15/18-point differences; score/alignment still block. No fabricated final scores.')
    elif name == 'futures_contradiction':
        structure.update(futures_vwap=future+200, spot_change_percent=-1, future_change_percent=-1)
    elif name == 'disconnected':
        structure.update(spot=None, future=None, stale=True)
        options.update(stale=True)
        breadth.update(stale=True)
        vix.update(level=None, stale=True)
        for key in ('atm_ce', 'atm_pe'):
            options[key].update(stale=True)
    elif name.startswith('missing_'):
        if name == 'missing_vix':
            vix['level'] = None
        else:
            structure[name.removeprefix('missing_')] = None
    elif name == 'partial_candles':
        for interval in ('5m', '15m'):
            structure[interval]['partial'] = True
    return Scenario(name, SignalInput(index, NOW.isoformat(), structure, options, vix, breadth), rows, tuple(notes))


def timestamp_replay():
    original = baseline()
    received = datetime.fromisoformat(original['atm_ce']['received_at'])
    # Replay the SDK's naive REST wall clock before normalization. Do not rewrite
    # the already-aware erroneous timestamp in the preserved historical response.
    raw = {'last_price': original['atm_ce']['ltp'], 'oi': original['atm_ce']['oi'],
           'volume': original['atm_ce']['volume'], 'depth': original['atm_ce']['depth'],
           'timestamp': datetime(2026, 9, 28, 10, 27)}
    corrected = normalize_quote(raw, received, 'rest')
    old = deepcopy(corrected)
    old['timestamp'] = raw['timestamp'].replace(tzinfo=timezone.utc).isoformat()
    return {'corrected': corrected, 'old': old, 'corrected_fresh': quote_fresh(corrected), 'old_fresh': quote_fresh(old)}


def ordering_replay():
    sample = baseline()['atm_ce']
    contract = OptionContract(sample['index'], date.fromisoformat(sample['expiry']), sample['strike'],
                              sample['option_type'], sample['lot_size'], sample['instrument_token'], sample['trading_symbol'])
    book = OptionBook()
    quote = timestamp_replay()['corrected']
    book.update(contract, quote, NOW)
    newer = dict(quote, timestamp=NOW.isoformat(), ltp=120)
    book.update(contract, newer, NOW)
    book.update(contract, dict(quote, ltp=1), NOW)
    retained = book.row(contract)
    future_rows = {str(seconds): dict(newer, timestamp=(NOW+timedelta(seconds=seconds)).isoformat()) for seconds in (5, 5.001)}
    return {'retained_ltp': retained['ltp'], 'retained_timestamp': retained['timestamp'],
            'within_tolerance': quote_fresh(future_rows['5']), 'beyond_tolerance': quote_fresh(future_rows['5.001'])}


def future_cache_replay():
    row = baseline()['atm_ce']
    contract = OptionContract(row['index'], date.fromisoformat(row['expiry']), row['strike'],
                              row['option_type'], row['lot_size'], row['instrument_token'], row['trading_symbol'])
    book = OptionBook()
    normal = dict(timestamp_replay()['corrected'], timestamp=NOW.isoformat())
    book.update(contract, normal, NOW)
    bad = dict(normal, timestamp=(NOW+timedelta(hours=1)).isoformat(), ltp=999)
    book.update(contract, bad, NOW)
    book.update(contract, dict(normal, timestamp=(NOW+timedelta(seconds=1)).isoformat(), ltp=110), NOW+timedelta(seconds=1))
    retained = book.row(contract)
    return {'retained_ltp': retained['ltp'], 'retained_timestamp': retained['timestamp'],
            'fresh': quote_fresh(retained, NOW+timedelta(seconds=1)),
            'valid_later_quote_retained': retained['ltp'] == 110}


def candle_replay(interval='5m'):
    size = int(interval[:-1])
    rows = [dict(symbol='NIFTY 50', interval=interval, open=100+i, high=102+i, low=99+i, close=101+i,
                 start_time=(NOW-timedelta(minutes=size*(21-i))).isoformat(),
                 end_time=(NOW-timedelta(minutes=size*(20-i))).isoformat(), completed=True, partial=False)
            for i in range(20)]
    dirty = rows + [dict(rows[-1], partial=True, close=999999), dict(rows[-1], completed=False, close=1)]
    return {'clean': momentum(rows), 'with_partial_and_forming': momentum(dirty)}


def decision_report(name):
    scenario = build(name)
    result = SignalEngine().decide(scenario.snapshot)
    return {'scenario': name, 'kind': 'signal', 'as_of': scenario.snapshot.as_of,
            'notes': scenario.notes, **asdict(result), 'ui_signal': signal_view(scenario),
            'ui_options': scenario.snapshot.options}


def signal_view(scenario):
    """Use the production UI adapter with no active plan; qualification is not entry."""
    from app.signals.execution_models import ExecutionConfig
    from app.signals.lifecycle import Submission
    from app.signals.live import LiveSignals
    stream = SimpleNamespace(state=SimpleNamespace(clock=lambda: NOW))
    live = LiveSignals(stream, None, None, None, journal_path=':memory:', execution_config=ExecutionConfig())
    try:
        live._publish(scenario.snapshot.index_name, scenario.snapshot,
                      Submission('NO_TRADE', None, False, ('Local scoring replay; no structural plan submitted.',)))
        return live.current(scenario.snapshot.index_name)
    finally:
        live.close()


def disconnected_options_replay():
    """Real OptionsService rejects cached quotes after the session disappears."""
    from app.session import SessionStore
    scenario = build('healthy')
    spot = scenario.snapshot.structure['spot']
    stream = SimpleNamespace(resolver=SimpleNamespace(clock=lambda: NOW),
                             set_option_consumer=lambda _: None,
                             state=SimpleNamespace(snapshot=lambda: {'instruments': [dict(
                                 trading_symbol='NIFTY 50', last_price=spot, last_tick_received_at=NOW.isoformat())]}))
    session = SessionStore(clock=lambda: NOW)
    session.save('LOCAL_SIMULATION_ONLY')
    def no_broker():
        raise AssertionError('The local simulator must not create a broker client.')
    options = OptionsService(stream, session, no_broker, ':memory:', config=OPTIONS_CONFIG, clock=lambda: NOW)
    try:
        options._check_session()
        expiry = date.fromisoformat(scenario.snapshot.options['selected_expiry'])
        options.discovery.contracts = tuple(OptionContract(r['index'], expiry, r['strike'], r['option_type'],
            r['lot_size'], r['instrument_token'], r['trading_symbol']) for r in scenario.rows)
        options.snapshots[('NIFTY', expiry)] = {'at': NOW.isoformat(),
            'quotes': {'NFO:'+r['trading_symbol']: deepcopy(r) for r in scenario.rows}}
        before, _ = options.response('NIFTY')
        session.clear()  # Leave cached quotes in place to exercise the read-time auth guard.
        after, rows = options.response('NIFTY')
        return {'before_coverage': before['coverage'], 'after_coverage': after['coverage'],
                'stale': after['stale'], 'all_quotes_stale': all(r['stale'] for r in rows),
                'atm_positioning': after['atm_ce']['positioning']}
    finally:
        options.shutdown()
