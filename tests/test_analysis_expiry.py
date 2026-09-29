"""Deterministic listed-metadata policy and cross-service expiry contracts."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import Mock
import time

import pytest

from app.analytics.session import IST
from app.options.discovery import OptionDiscovery
from app.signals import SignalEngine
from app.signals.execution_models import ExecutionConfig
from app.signals.lifecycle import Submission
from app.signals.live import LiveSignals
from app.signals.planning import SignalPlanner
from app.signals.selection import select_option
from test_decisions import ready
from test_execution import example
from test_options import service  # Shared real OptionsService fixture; broker is mocked.


def catalog(stamp, expiries, monthlies=(), index='NIFTY'):
    rows = []
    for value in expiries:
        for kind in ('CE', 'PE'):
            rows.append(dict(name=index, expiry=date.fromisoformat(value), strike=100, lot_size=25,
                             instrument_token=len(rows)+1, tradingsymbol=f'{index}{value}{kind}',
                             instrument_type=kind, segment='NFO-OPT'))
    for value in monthlies:
        rows.append(dict(name=index, expiry=date.fromisoformat(value), instrument_type='FUT', segment='NFO-FUT',
                         instrument_token=len(rows)+1, tradingsymbol=f'{index}{value}FUT'))
    resolver = Mock()
    resolver.clock.return_value = datetime.fromisoformat(stamp)
    resolver.find.return_value = rows
    discovery = OptionDiscovery(resolver)
    discovery.refresh(Mock())
    return discovery


@pytest.mark.parametrize('day,expected,policy', [
    ('28', '2026-10-06', 'NIFTY_NEXT_WEEK_EXPIRY'),
    ('29', '2026-10-06', 'NIFTY_NEXT_WEEK_EXPIRY'),
    ('30', '2026-10-06', 'NIFTY_NEAREST_EXPIRY'),
    ('01', '2026-10-06', 'NIFTY_NEAREST_EXPIRY'),
    ('02', '2026-10-06', 'NIFTY_NEAREST_EXPIRY'),
])
def test_nifty_week_is_stable(day, expected, policy):
    month = '09' if int(day) >= 28 else '10'
    discovery = catalog(f'2026-{month}-{day}T10:00:00+05:30', ['2026-09-29', '2026-10-06', '2026-10-13'])
    result = discovery.selections('NIFTY')
    assert result['analysis_expiry'] == date.fromisoformat(expected)
    assert result['analysis_expiry_policy']['code'] == policy
    assert result['analysis_expiry_policy']['timezone'] == 'Asia/Kolkata'


@pytest.mark.parametrize('day', ['28', '29'])
def test_nifty_missing_next_is_unavailable(day):
    result = catalog(f'2026-09-{day}T10:00:00+05:30', ['2026-09-29']).selections('NIFTY')
    assert result['nearest'] == date(2026, 9, 29)
    assert result['analysis_expiry'] is None
    assert result['analysis_expiry_policy']['available'] is False
    assert 'unavailable' in result['analysis_expiry_policy']['reason']


@pytest.mark.parametrize('stamp,expected', [
    ('2026-09-27T20:00:00+00:00', '2026-10-06'),  # Sunday UTC is Monday IST.
    ('2026-09-29T20:00:00+00:00', '2026-10-06'),  # Tuesday UTC is Wednesday IST.
])
def test_policy_uses_ist_not_clock_timezone(stamp, expected):
    discovery = catalog(stamp, ['2026-09-29', '2026-10-06', '2026-10-13'])
    assert discovery.selections('NIFTY')['analysis_expiry'] == date.fromisoformat(expected)


def test_policy_rejects_naive_clock():
    with pytest.raises(ValueError, match='Timezone-aware'):
        catalog('2026-09-28T10:00:00', ['2026-09-29'])


def test_expired_dates_excluded_without_metadata_refresh():
    discovery = catalog('2026-09-29T15:29:59+05:30', ['2026-09-22', '2026-09-29', '2026-10-06', '2026-10-13'])
    assert date(2026, 9, 22) not in discovery.get_expiries('NIFTY')
    discovery.resolver.clock.return_value += timedelta(seconds=1)
    assert discovery.selections('NIFTY')['nearest'] == date(2026, 10, 6)
    assert discovery.chain('NIFTY', date(2026, 9, 29)) == []


@pytest.mark.parametrize('stamp', ['2026-09-28T10:00:00+05:30', '2026-09-29T10:00:00+05:30'])
def test_nifty_uses_next_listed_date_even_when_not_tuesday(stamp):
    result = catalog(stamp, ['2026-09-30', '2026-10-05']).selections('NIFTY')
    assert result['analysis_expiry'] == date(2026, 10, 5)


@pytest.mark.parametrize('stamp,expected,code', [
    ('2026-09-16', '2026-09-29', 'BANKNIFTY_NEAREST_MONTHLY_EXPIRY'),
    ('2026-09-21', '2026-09-29', 'BANKNIFTY_NEAREST_MONTHLY_EXPIRY'),
    ('2026-09-22', '2026-09-29', 'BANKNIFTY_NEAREST_MONTHLY_EXPIRY'),
    ('2026-09-28', '2026-10-27', 'BANKNIFTY_NEXT_MONTHLY_EXPIRY'),
    ('2026-09-29', '2026-10-27', 'BANKNIFTY_NEXT_MONTHLY_EXPIRY'),
    ('2026-09-30', '2026-10-27', 'BANKNIFTY_NEAREST_MONTHLY_EXPIRY'),
])
def test_banknifty_monthly_policy(stamp, expected, code):
    monthlies = ['2026-09-29', '2026-10-27', '2026-11-24']
    discovery = catalog(stamp+'T10:00:00+05:30', ['2026-09-17', '2026-09-24', '2026-10-06', *monthlies], monthlies, 'BANKNIFTY')
    result = discovery.selections('BANKNIFTY')
    assert result['analysis_expiry'] == date.fromisoformat(expected)
    assert result['analysis_expiry_policy']['code'] == code


@pytest.mark.parametrize('stamp,monthly', [
    ('2026-09-25', '2026-09-28'),  # Monday shifted expiry; Friday is previous weekday.
    ('2026-09-28', '2026-09-28'),
    ('2026-09-29', '2026-09-30'),  # Listed Wednesday, not assumed Tuesday.
    ('2026-09-30', '2026-09-30'),
    ('2026-09-24', '2026-09-25'),  # Larger holiday adjustment follows listed date.
])
def test_banknifty_holiday_adjusted_final_window(stamp, monthly):
    dates = [monthly, '2026-10-27']
    result = catalog(stamp+'T10:00:00+05:30', dates, dates, 'BANKNIFTY').selections('BANKNIFTY')
    assert result['analysis_expiry'] == date(2026, 10, 27)


@pytest.mark.parametrize('monthlies', [[], ['2026-09-29']])
def test_banknifty_never_falls_back_to_weekly_or_guessed_monthly(monthlies):
    discovery = catalog('2026-09-28T10:00:00+05:30', ['2026-09-29', '2026-10-06', '2026-10-27'], monthlies, 'BANKNIFTY')
    result = discovery.selections('BANKNIFTY')
    assert result['analysis_expiry'] is None
    assert result['analysis_expiry_policy']['available'] is False


def test_automatic_rest_stream_summary_and_manual_requests_stay_separate(service):
    options, client, clock = service
    clock.return_value = datetime(2026, 9, 28, 10, tzinfo=IST)
    options.session.save('dummy-session')
    options._context = lambda index: (100, None)
    options.cycle(force=True)
    assert client.quote.call_count == 2  # One full batch per automatic index.
    for index in ('NIFTY', 'BANKNIFTY'):
        summary, rows = options.response(index)
        assert summary['selected_expiry'] == summary['expiry_selection']['analysis_expiry'] == '2026-09-30'
        assert {r['expiry'] for r in rows} == {'2026-09-30'}
        assert summary['atm_ce']['expiry'] == summary['atm_pe']['expiry'] == '2026-09-30'
        assert summary['pcr_oi'] == 2 and summary['max_pain'] is not None
    assert {c.expiry for c in options.stream.set_option_contracts.call_args.args[0]} == {date(2026, 9, 30)}
    metadata_calls, quote_calls = client.instruments.call_count, client.quote.call_count
    manual, _ = options.response('NIFTY', date(2026, 9, 28), inspection=True)
    assert manual['selected_expiry'] == '2026-09-28' and manual['stale']
    assert manual['expiry_selection']['analysis_expiry'] == '2026-09-30'
    options.response('NIFTY')  # Signals must neither consume nor cancel manual inspection.
    assert options.inspected['NIFTY'] == date(2026, 9, 28)
    assert client.quote.call_count == quote_calls and client.instruments.call_count == metadata_calls
    options.cycle(force=True)
    assert client.quote.call_count == quote_calls + 3  # Two automatic + one explicit, no duplicate.
    assert not options.response('NIFTY', date(2026, 9, 28), inspection=True)[0]['stale']
    options.response('NIFTY', inspection=True)  # UI returned to automatic.
    options.cycle(force=True)
    assert client.quote.call_count == quote_calls + 5
    assert ('NIFTY', date(2026, 9, 28)) not in options.snapshots
    assert client.instruments.call_count == metadata_calls
    assert options.database.db.execute('SELECT DISTINCT expiry FROM option_chain_snapshots').fetchall() == [('2026-09-30',)]
    options._last_refresh = time.monotonic()
    options.cycle()
    assert client.quote.call_count == quote_calls + 5


def test_auto_inspection_does_not_cancel_internal_contract_observations(service):
    options, client, _ = service
    options.response('NIFTY', date(2026, 9, 30))  # Existing internal trade observation API.
    options.response('NIFTY', date(2026, 9, 30), inspection=True)
    options.cycle(force=True)
    assert client.quote.call_count == 3  # The shared explicit expiry is refreshed once.
    options.response('NIFTY', inspection=True)
    options.cycle(force=True)
    assert client.quote.call_count == 6
    assert options.requested['NIFTY'] == date(2026, 9, 30)
    assert 'NIFTY' not in options.inspected


def test_unavailable_policy_does_not_reuse_old_or_manual_chain(service):
    options, client, clock = service
    options.cycle(force=True)
    options.response('NIFTY', date(2026, 9, 30))
    clock.return_value = datetime(2026, 9, 28, 10, tzinfo=IST)
    options.session.save('dummy-session')
    client.instruments.return_value = [r for r in client.instruments.return_value if r['expiry'] == date(2026, 9, 30)]
    options.cycle(force=True)
    summary, rows = options.response('NIFTY')
    assert summary['selected_expiry'] is None and summary['stale'] and rows == []
    assert summary['pcr_oi'] is None and summary['atm_ce'] is None
    assert not any(c.index == 'NIFTY' for c in options.active_contracts.values())
    assert options.response('NIFTY', date(2026, 9, 30))[0]['selected_expiry'] == '2026-09-30'


def test_live_evaluation_uses_one_summary_and_its_contracts(service):
    options, client, clock = service
    clock.return_value = datetime(2026, 9, 28, 10, tzinfo=IST)
    options.session.save('dummy-session')
    options.cycle(force=True)
    options.response('NIFTY', date(2026, 9, 28))
    stream, structure, breadth = Mock(), Mock(), Mock()
    stream.state.clock = clock
    stream.live.return_value = {'instruments': []}
    structure.structure.return_value = {'as_of': clock().isoformat(), 'nifty': {}}
    structure.aggregator.candles.return_value = []
    breadth.snapshot.return_value = {}
    options.response = Mock(wraps=options.response)
    live = LiveSignals(stream, structure, options, breadth)
    live.lifecycle.submit = Mock(return_value=Submission('NO_TRADE', None, False, ('Direction does not qualify',)))
    try:
        broker_calls = client.quote.call_count
        live.evaluate('NIFTY')
        options.response.assert_called_once_with('NIFTY')
        snapshot, rows = live.lifecycle.submit.call_args.args
        assert snapshot.options['selected_expiry'] == '2026-09-30'
        assert {r['expiry'] for r in rows} == {'2026-09-30'}
        assert live.current('NIFTY')['expiry_selection']['analysis_expiry'] == '2026-09-30'
        assert client.quote.call_count == broker_calls
    finally:
        live.close()


@pytest.mark.parametrize('call', [True, False])
def test_qualified_option_selection_uses_analysis_expiry(call):
    snapshot, rows, engine = example(call)
    snapshot.options['selected_expiry'] = '2026-10-06'
    snapshot.options['expiry_selection']['analysis_expiry'] = '2026-10-06'
    analysis_rows = [dict(r, expiry='2026-10-06', instrument_token=r['instrument_token']+1000,
                          trading_symbol='NEXT'+r['trading_symbol']) for r in rows]
    plan = SignalPlanner(engine).build(snapshot, rows+analysis_rows)
    assert plan.actionable
    assert plan.plan.option.expiry == '2026-10-06'
    assert plan.plan.option.trading_symbol.startswith('NEXT')
    snapshot.options['selected_expiry'] = '2026-09-28'
    assert select_option(snapshot, 'CALL' if call else 'PUT', rows+analysis_rows, ExecutionConfig()) is None


def test_manual_summary_cannot_qualify_for_automatic_signals():
    snapshot = ready()
    snapshot.options.update(selected_expiry='2026-09-29', expiry_selection={'analysis_expiry': '2026-10-06'})
    decision = SignalEngine().decide(snapshot)
    assert decision.decision == 'NO_TRADE'
    assert next(g for g in decision.qualification_gates if g['key'] == 'analysis_expiry')['status'] == 'BLOCK'
