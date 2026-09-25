from dataclasses import asdict, replace
from datetime import datetime, timedelta, time
from unittest.mock import Mock
import json

import pytest

from app.signals.execution_models import ExecutionConfig, instant
from app.signals.planning import SignalPlanner
from app.signals.selection import select_option
from app.signals.lifecycle import SignalJournal, SignalLifecycle
from app.signals.structure import confirmed_swings
from test_decisions import ready


def example(call=True):
    snapshot = ready(call)
    snapshot.structure.update(spot=99.9 if call else 100.1,
        opening_range_high=100 if call else 102, opening_range_low=98 if call else 100,
        previous_day_high=110, previous_day_low=90, day_high=120 if call else 105,
        day_low=95 if call else 80, recent_swing_low=99, recent_swing_high=101)
    if call:
        snapshot.structure.pop('recent_swing_high')
    else:
        snapshot.structure.pop('recent_swing_low')
    snapshot.options.update(selected_expiry='2026-09-28', expiry_selection={'nearest': '2026-09-28'},
                            coverage={'expected_contracts': 6, 'received_contracts': 6})
    rows = []
    for strike in (95, 100, 105):
        for kind in ('CE', 'PE'):
            rows.append(dict(index='NIFTY', expiry='2026-09-28', strike=strike, option_type=kind,
                instrument_token=100+len(rows), trading_symbol=f'NIFTY{strike}{kind}', stale=False,
                bid=10, ask=10.05, ltp=10, bid_quantity=100, ask_quantity=100, oi=2000, volume=200,
                liquidity_state='LIQUID', timestamp=snapshot.as_of, received_at=snapshot.as_of))
    engine = Mock()
    engine.decide.return_value.decision = 'CALL' if call else 'PUT'
    return snapshot, rows, engine


def move(snapshot, rows, seconds, price=None):
    now = instant(snapshot.as_of)+timedelta(seconds=seconds)
    stamp = now.isoformat()
    structure = dict(snapshot.structure, as_of=stamp)
    if price is not None:
        structure['spot'] = price
    new = replace(snapshot, as_of=stamp, structure=structure,
        options=dict(snapshot.options, last_full_chain_refresh_at=stamp),
        breadth=dict(snapshot.breadth, as_of=stamp), volatility=dict(snapshot.volatility, as_of=stamp))
    return new, [dict(r, timestamp=stamp, received_at=stamp) for r in rows]


def candle(snapshot, call=True, **changes):
    end = instant(snapshot.as_of)
    return dict(symbol='NIFTY 50', interval='5m', start_time=(end-timedelta(minutes=5)).isoformat(),
                end_time=end.isoformat(), completed=True, partial=False,
                **(dict(open=99.9, high=101.2, low=99.5, close=101) if call else dict(open=100.1, high=100.5, low=98.8, close=99)), **changes)


@pytest.fixture
def lifecycle():
    snapshot, rows, engine = example()
    manager = SignalLifecycle(SignalPlanner(engine))
    yield manager, snapshot, rows
    manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_structural_trigger_targets_and_rr(call):
    snapshot, rows, engine = example(call)
    result = SignalPlanner(engine).build(snapshot, rows)
    assert result.actionable
    plan = result.plan
    assert plan.entry_trigger.type == ('breakout' if call else 'breakdown')
    assert plan.entry_trigger.level == 100
    assert plan.entry_trigger.confirmation == ('5m_close_above' if call else '5m_close_below')
    assert plan.invalidation.level == (99 if call else 101)
    assert (plan.target1.level, plan.target2.level) == ((110, 120) if call else (90, 80))
    assert plan.t1_rr == 10 and plan.t2_rr == 20
    json.dumps(asdict(result), allow_nan=False)


def test_low_rr_and_missing_targets_fail_closed():
    sample, rows, engine = example()
    sample.structure['previous_day_high'] = 101
    result = SignalPlanner(engine).build(sample, rows)
    assert result.decision == 'NO_TRADE' and 'risk/reward' in result.reasons[0]
    sample.structure.pop('previous_day_high')
    sample.structure.pop('day_high')
    result = SignalPlanner(engine).build(sample, rows)
    assert not result.actionable and 'target unavailable' in result.reasons[0]


@pytest.mark.parametrize('call,kind,itm', [(True, 'CE', 95), (False, 'PE', 105)])
def test_option_selection_atm_then_one_step_itm(call, kind, itm):
    sample, rows, _ = example(call)
    selected = select_option(sample, 'CALL' if call else 'PUT', rows, ExecutionConfig())
    assert selected.strike == 100 and selected.option_type == kind
    for row in rows:
        if row['strike'] == 100 and row['option_type'] == kind:
            row['oi'] = 0
    selected = select_option(sample, 'CALL' if call else 'PUT', rows, ExecutionConfig())
    assert selected.strike == itm
    for row in rows:
        if row['strike'] == itm:
            row['volume'] = 0
    assert select_option(sample, 'CALL' if call else 'PUT', rows, ExecutionConfig()) is None


@pytest.mark.parametrize('field,value', [('stale', True), ('bid', 0), ('ask', 20), ('oi', 0),
    ('volume', 0), ('bid_quantity', 0), ('ltp', float('nan')), ('ask', float('inf')),
    ('liquidity_state', 'POOR'), ('received_at', '2026-09-25T09:59:00+05:30'),
    ('timestamp', '2026-09-25T10:01:00+05:30')])
def test_illiquid_or_stale_contracts_rejected(field, value):
    sample, rows, engine = example()
    for row in rows:
        row[field] = value
    assert SignalPlanner(engine).build(sample, rows).decision == 'NO_TRADE'


def test_contract_selection_after_direction_only():
    sample, _, engine = example()
    engine.decide.return_value.decision = 'NO_TRADE'
    # Invalid iterable would fail if inspected before direction qualified.
    assert SignalPlanner(engine).build(sample, None).decision == 'NO_TRADE'


def test_confirmation_requires_new_complete_closed_bar(lifecycle):
    manager, sample, rows = lifecycle
    created = manager.submit(sample, rows)
    assert created.created and created.record.state == 'CANDIDATE'
    newer, quotes = move(sample, rows, 300, 101)
    bar = candle(newer)
    partial = dict(bar, partial=True)
    assert manager.advance(created.record.signal_id, newer, quotes, partial).state == 'CANDIDATE'
    newer, quotes = move(newer, quotes, 1, 101)
    confirmed = manager.advance(created.record.signal_id, newer, quotes, bar)
    assert confirmed.state == 'CONFIRMED'
    assert confirmed.confirmation_price == 101
    assert confirmed.confirmed_t1_rr == 4.5


def test_invalidation_before_confirmation(lifecycle):
    manager, sample, rows = lifecycle
    record = manager.submit(sample, rows).record
    newer, quotes = move(sample, rows, 1, 99)
    invalid = manager.advance(record.signal_id, newer, quotes)
    assert invalid.state == 'INVALIDATED'


def confirm(manager, sample, rows):
    record = manager.submit(sample, rows).record
    newer, quotes = move(sample, rows, 300, 101)
    confirmed = manager.advance(record.signal_id, newer, quotes, candle(newer))
    assert confirmed.state == 'CONFIRMED'
    return confirmed, newer, quotes


def test_target_lifecycle_and_terminal_immutability(lifecycle):
    manager, sample, rows = lifecycle
    record, sample, rows = confirm(manager, sample, rows)
    newer, quotes = move(sample, rows, 1, 110)
    assert manager.advance(record.signal_id, newer, quotes).state == 'TARGET1_HIT'
    newer, quotes = move(newer, quotes, 1, 120)
    record = manager.advance(record.signal_id, newer, quotes)
    assert record.state == 'TARGET2_HIT'
    newer, quotes = move(newer, quotes, 1, 90)
    assert manager.advance(record.signal_id, newer, quotes) == record


def test_stop_and_ambiguous_candle_stop_first(lifecycle):
    manager, sample, rows = lifecycle
    record, sample, rows = confirm(manager, sample, rows)
    newer, quotes = move(sample, rows, 300, 105)
    bar = dict(candle(newer), open=101, low=98, high=121, close=105)
    assert manager.advance(record.signal_id, newer, quotes, bar).state == 'STOPPED'


def test_duplicate_suppression_and_durable_restart(tmp_path):
    sample, rows, engine = example()
    path = tmp_path/'signals.sqlite3'
    manager = SignalLifecycle(SignalPlanner(engine), SignalJournal(path))
    first = manager.submit(sample, rows)
    assert not manager.submit(sample, rows).created
    manager.close()
    restored = SignalLifecycle(SignalPlanner(engine), SignalJournal(path))
    try:
        duplicate = restored.submit(sample, rows)
        assert duplicate.record == first.record and not duplicate.created
        newer, quotes = move(sample, rows, 1, 99)
        restored.advance(first.record.signal_id, newer, quotes)
        newer, quotes = move(newer, quotes, 1, 99.9)
        assert not restored.submit(newer, quotes).created
    finally:
        restored.close()


def test_candidate_expiry(lifecycle):
    manager, sample, rows = lifecycle
    record = manager.submit(sample, rows).record
    newer, quotes = move(sample, rows, 900)
    assert manager.advance(record.signal_id, newer, quotes).state == 'EXPIRED'


def test_cutoff_ist_and_pending_expiry():
    sample, rows, engine = example()
    later, quotes = move(sample, rows, 5*3600)
    assert SignalPlanner(engine).build(later, quotes).decision == 'NO_TRADE'
    # Same moment represented as UTC is also rejected.
    later = replace(later, as_of='2026-09-25T09:30:00+00:00')
    assert SignalPlanner(engine).build(later, quotes).decision == 'NO_TRADE'
    before, quotes = move(sample, rows, 5*3600-60)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(before, quotes).record
        assert instant(record.expires_at).time() == time(15)
        assert manager.advance(record.signal_id, later, quotes).state == 'EXPIRED'
    finally:
        manager.close()


def test_confirmation_gap_rechecks_rr(lifecycle):
    manager, sample, rows = lifecycle
    record = manager.submit(sample, rows).record
    newer, quotes = move(sample, rows, 300, 102)
    # Set actual close to 109; ATM stays 100 relative to spot but R:R fails.
    bar = dict(candle(newer), close=109, high=109)
    assert manager.advance(record.signal_id, newer, quotes, bar).state == 'INVALIDATED'


def test_retest_and_oi_wall_price_confirmation():
    sample, rows, engine = example()
    sample.structure['spot'] = 100.2
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        assert record.plan.entry_trigger.type == 'breakout_retest'
        newer, quotes = move(sample, rows, 300, 101)
        bar = dict(candle(newer), open=100.5, low=100)
        assert manager.advance(record.signal_id, newer, quotes, bar).state == 'CONFIRMED'
    finally:
        manager.close()
    sample.structure['spot'] = 99.9
    sample.options.update(call_oi_wall=100, top_3_call_oi=[{'strike': 100, 'value': 1000}])
    assert SignalPlanner(engine).build(sample, rows).plan.entry_trigger.type == 'oi_wall_break'


def test_futures_vwap_never_mixes_spot_price_levels():
    sample, rows, engine = example()
    sample = replace(sample, structure=dict(spot=99.9, future=199.9, future_symbol='NIFTYFUT',
        stale=False, as_of=sample.as_of, future_structure=dict(symbol='NIFTYFUT', price=199.9,
            vwap=200, recent_swing_low=199, previous_day_high=210, day_high=220)))
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        assert record.plan.entry_trigger.type == 'vwap_reclaim'
        assert record.plan.entry_trigger.instrument == 'NIFTYFUT'
        assert record.plan.invalidation.level == 199
        assert record.plan.target1.level == 210
        newer, quotes = move(sample, rows, 300)
        newer.structure['future'] = 201
        bar = dict(candle(newer), symbol='NIFTYFUT', open=199.9, low=199.5, high=201.2, close=201)
        assert manager.advance(record.signal_id, newer, quotes, bar).state == 'CONFIRMED'
    finally:
        manager.close()


def test_put_confirmation_and_target():
    sample, rows, engine = example(False)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        newer, quotes = move(sample, rows, 300, 99)
        assert manager.advance(record.signal_id, newer, quotes, candle(newer, False)).state == 'CONFIRMED'
        newer, quotes = move(newer, quotes, 1, 80)
        result = manager.advance(record.signal_id, newer, quotes)
        assert result.state == 'TARGET2_HIT'
        assert [e.state for e in result.history][-2:] == ['TARGET1_HIT', 'TARGET2_HIT']
    finally:
        manager.close()


def test_confirmation_rechecks_option_and_direction(lifecycle):
    manager, sample, rows = lifecycle
    record = manager.submit(sample, rows).record
    newer, quotes = move(sample, rows, 300, 101)
    for row in quotes:
        row['oi'] = 0
    assert manager.advance(record.signal_id, newer, quotes, candle(newer)).state == 'INVALIDATED'


def test_stale_and_out_of_order_do_not_trigger(lifecycle):
    manager, sample, rows = lifecycle
    record = manager.submit(sample, rows).record
    newer, quotes = move(sample, rows, 300, 101)
    newer.structure['stale'] = True
    assert manager.advance(record.signal_id, newer, quotes, candle(newer)).state == 'CANDIDATE'
    older, quotes = move(sample, rows, 1, 90)
    assert manager.advance(record.signal_id, older, quotes).state == 'CANDIDATE'


def test_confirmed_session_expiry(lifecycle):
    manager, sample, rows = lifecycle
    record, sample, rows = confirm(manager, sample, rows)
    newer, quotes = move(sample, rows, 5*3600+25*60)
    assert instant(newer.as_of).time() == time(15, 30)
    assert manager.advance(record.signal_id, newer, quotes).state == 'EXPIRED'


def test_swing_requires_right_hand_completed_bars():
    sample, _, _ = example()
    end = instant(sample.as_of)
    rows = []
    for i, high in enumerate((102, 103, 110, 103, 102)):
        start = end-timedelta(minutes=25-5*i)
        rows.append(dict(symbol='NIFTY 50', interval='5m', start_time=start.isoformat(),
                         end_time=(start+timedelta(minutes=5)).isoformat(), completed=True, partial=False,
                         open=100, high=high, low=99, close=100))
    assert confirmed_swings(rows, end, 'NIFTY 50')['recent_swing_high'] == 110
    assert 'recent_swing_high' not in confirmed_swings(rows, end-timedelta(minutes=5), 'NIFTY 50')
    rows[3]['partial'] = True
    assert 'recent_swing_high' not in confirmed_swings(rows, end, 'NIFTY 50')


@pytest.mark.parametrize('changes', [dict(minimum_t1_rr=float('nan')), dict(candidate_lifetime_seconds=0),
    dict(max_spread_percent=-1), dict(new_entry_cutoff=time(16))])
def test_invalid_execution_config(changes):
    with pytest.raises(ValueError):
        ExecutionConfig(**changes)


def test_real_phase53_decision_precedes_candidate():
    from app.signals import SignalEngine
    sample, rows, _ = example()
    # Real scoring fixture qualifies before structural planning. Keep a tight
    # known support, a nearby broken previous high, and actual day-high target.
    qualified = ready()
    qualified.structure.update(recent_swing_low=105)
    qualified.options.update(sample.options)
    # Index spot 110 selects 110 ATM from complete metadata; shift all strikes.
    rows = [dict(row, strike=row['strike']+10) for row in rows]
    assert SignalEngine().decide(qualified).decision == 'CALL'
    result = SignalPlanner().build(qualified, rows)
    assert result.actionable and result.plan.entry_trigger.type == 'breakout_retest'


def test_live_evaluate_creates_and_advances_without_new_feed():
    from app.signals.live import LiveSignals
    sample, rows, engine = example()
    stream, structure, options = Mock(), Mock(), Mock()
    stream.state.clock.return_value = instant(sample.as_of)
    structure.aggregator.candles.return_value = []
    options.response.return_value = (sample.options, rows)
    live = LiveSignals(stream, structure, options, Mock(), execution_config=ExecutionConfig())
    live.snapshot = Mock(return_value=sample)
    live.lifecycle.planner.engine = engine
    try:
        first = live.evaluate('NIFTY')
        assert first.created and first.record.state == 'CANDIDATE'
        assert not live.evaluate('NIFTY').created
        newer, quotes = move(sample, rows, 300, 101)
        stream.state.clock.return_value = instant(newer.as_of)
        live.snapshot.return_value = newer
        options.response.return_value = (newer.options, quotes)
        structure.aggregator.candles.return_value = [candle(newer)]
        result = live.evaluate('NIFTY')
        assert result.record.state == 'CONFIRMED' and not result.created
        stream.start.assert_not_called()
    finally:
        live.close()


def test_execution_environment_configuration(monkeypatch):
    monkeypatch.setenv('SIGNAL_CANDIDATE_LIFETIME_SECONDS', '600')
    monkeypatch.setenv('SIGNAL_NEW_ENTRY_CUTOFF', '14:45')
    config = ExecutionConfig.load()
    assert config.candidate_lifetime_seconds == 600
    assert config.new_entry_cutoff == time(14, 45)
