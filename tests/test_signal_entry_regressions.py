"""Regressions from the 7–8 October history; quotes/candles are synthetic.

These test lifecycle policy, not historical profitability or recovered fills.
"""
from dataclasses import replace
from datetime import timedelta

import pytest

from app.signals.execution_models import ExecutionConfig, instant
from app.signals.lifecycle import SignalJournal, SignalLifecycle
from app.signals.planning import SignalPlanner
from app.signals.selection import select_option
from test_execution import example, move, candle
from test_early_trigger import minute_bar


def put_setup(at, trigger, stop, target, spot, index='NIFTY'):
    sample, rows, engine = example(False)
    stamp = instant(at).isoformat()
    step = 100 if index == 'BANKNIFTY' else 50
    center = round(spot / step) * step
    sample = replace(sample, index_name=index, as_of=stamp,
        structure=dict(spot=spot, future=spot+10, as_of=stamp, stale=False,
                       opening_range_low=trigger, recent_swing_high=stop, day_low=target),
        options=dict(selected_expiry='2026-10-27', expiry_selection={'analysis_expiry':'2026-10-27'},
                     coverage={'expected_contracts':6}))
    for row in rows:
        row.update(index=index, expiry='2026-10-27', timestamp=stamp, received_at=stamp,
                   strike=center+(row['strike']-100)/5*step)
        row['trading_symbol'] = f"{index}{row['strike']}{row['option_type']}"
    manager = SignalLifecycle(SignalPlanner(engine))
    record = manager.submit(sample, rows).record
    assert record is not None
    return manager, sample, rows, record


def test_oct8_late_breach_gets_bounded_time_to_confirm():
    manager, sample, rows, record = put_setup('2026-10-08T12:05:06.453267+05:30',
        22328.45, 22382.75, 22200, 22340)
    try:
        tick = instant('2026-10-08T12:20:02+05:30')
        breached = manager.observe_tick('NIFTY 50', 22327.2, tick, tick)
        assert instant(breached.expires_at) == instant('2026-10-08T12:26:00+05:30')
        later, quotes = move(sample, rows, 901, 22327.2)
        assert manager.advance(record.signal_id, later, quotes).state == 'CANDIDATE'
        end = instant('2026-10-08T12:25:00+05:30')
        later, quotes = move(sample, rows, (end-instant(sample.as_of)).total_seconds()+1, 22327.2)
        bar = dict(candle(later, False), start_time=(end-timedelta(minutes=5)).isoformat(),
                   end_time=end.isoformat(), open=22330, high=22331, low=22326, close=22327.2)
        assert manager.advance(record.signal_id, later, quotes, bar).state == 'CONFIRMED'
        assert manager.journal.history(confirmed_only=True)['total'] == 1
    finally:
        manager.close()


@pytest.mark.parametrize('received_late', [False, True])
def test_expired_candidate_cannot_be_resurrected_by_ticks(received_late):
    sample, rows, engine = example()
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        expiry = instant(record.expires_at)
        at = expiry-timedelta(seconds=1) if received_late else expiry
        assert manager.observe_tick('NIFTY 50', 101, at, expiry) == record
        later, quotes = move(sample, rows, 901, 101)
        assert manager.advance(record.signal_id, later, quotes).state == 'EXPIRED'
    finally:
        manager.close()


def test_breach_extension_survives_restart_and_does_not_roll_forever(tmp_path):
    sample, rows, engine = example()
    path = tmp_path/'signals.sqlite3'
    manager = SignalLifecycle(SignalPlanner(engine), SignalJournal(path))
    record = manager.submit(sample, rows).record
    at = instant(sample.as_of)+timedelta(seconds=899)
    saved = manager.observe_tick('NIFTY 50', 101, at, at)
    deadline = saved.expires_at
    manager.close()
    manager = SignalLifecycle(SignalPlanner(engine), SignalJournal(path))
    try:
        later, quotes = move(sample, rows, 901, 99.9)
        manager.advance(record.signal_id, later, quotes)
        at = instant(later.as_of)+timedelta(seconds=1)
        breached = manager.observe_tick('NIFTY 50', 101, at, at)
        assert breached.expires_at == deadline
        assert breached.trigger_watch['confirmation_deadline'] == deadline
        later, quotes = move(sample, rows, (instant(deadline)-instant(sample.as_of)).total_seconds(), 101)
        assert manager.advance(record.signal_id, later, quotes).state == 'EXPIRED'
    finally:
        manager.close()


def test_breach_grace_never_crosses_entry_cutoff():
    manager, sample, rows, record = put_setup('2026-10-08T14:50:05+05:30',
        54568.85, 54589.15, 54383.15, 54570, 'BANKNIFTY')
    try:
        at = instant('2026-10-08T14:59:59+05:30')
        saved = manager.observe_tick('NIFTY BANK', 54560, at, at)
        assert instant(saved.expires_at) == instant('2026-10-08T15:00:00+05:30')
        later, quotes = move(sample, rows, 595, 54560)
        assert manager.advance(record.signal_id, later, quotes).state == 'EXPIRED'
    finally:
        manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_creation_candle_requires_post_creation_live_breach(call):
    original, rows, engine = example(call)
    sample, rows = move(original, rows, 6)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        end_snapshot, quotes = move(original, rows, 300, 101 if call else 99)
        bar = candle(end_snapshot, call)
        later, quotes = move(end_snapshot, quotes, 1)
        assert manager.advance(record.signal_id, later, quotes, bar).state == 'CANDIDATE'
    finally:
        manager.close()
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        at = instant(sample.as_of)+timedelta(seconds=1)
        manager.observe_tick('NIFTY 50', 100.5 if call else 99.5, at, at)
        assert manager.advance(record.signal_id, later, quotes, bar).state == 'CONFIRMED'
    finally:
        manager.close()


def test_creation_candle_cannot_use_breach_outside_its_time_or_price_range():
    sample, rows, engine = example()
    sample, rows = move(sample, rows, 6)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        at = instant(sample.as_of)+timedelta(seconds=10)
        manager.observe_tick('NIFTY 50', 101, at, at)
        later, quotes = move(sample, rows, 295, 100.5)
        end = instant(sample.as_of).replace(second=0)+timedelta(minutes=5)
        bar = dict(candle(later), start_time=(end-timedelta(minutes=5)).isoformat(),
                   end_time=end.isoformat(), high=100.8, close=100.5)
        assert manager.advance(record.signal_id, later, quotes, bar).state == 'CANDIDATE'
    finally:
        manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_current_price_must_still_be_beyond_trigger_at_5m_confirmation(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        later, quotes = move(sample, rows, 300, 99.9 if call else 100.1)
        assert manager.advance(record.signal_id, later, quotes, candle(later, call)).state == 'CANDIDATE'
    finally:
        manager.close()


def test_creation_candle_keeps_conservative_stop_first_rule():
    sample, rows, engine = example()
    sample, rows = move(sample, rows, 6)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        at = instant(sample.as_of)+timedelta(seconds=1)
        manager.observe_tick('NIFTY 50', 101, at, at)
        later, quotes = move(sample, rows, 294, 101)
        bar = dict(candle(later), low=98.5)
        assert manager.advance(record.signal_id, later, quotes, bar).state == 'INVALIDATED'
    finally:
        manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_existing_option_remains_eligible_when_it_becomes_one_step_itm(call):
    sample, rows, engine = example(call)
    direction = 'CALL' if call else 'PUT'
    config = ExecutionConfig()
    existing = select_option(sample, direction, rows, config)
    later, quotes = move(sample, rows, 300, 102.6 if call else 97.4)
    assert select_option(later, direction, quotes, config).instrument_token != existing.instrument_token
    assert select_option(later, direction, quotes, config, existing=existing).instrument_token == existing.instrument_token
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        bar = dict(candle(later, call), close=102.6 if call else 97.4,
                   high=102.7 if call else 100.5, low=99.5 if call else 97.3)
        confirmed = manager.advance(record.signal_id, later, quotes, bar)
        assert confirmed.state == 'CONFIRMED'
        assert confirmed.plan.option.instrument_token == existing.instrument_token
    finally:
        manager.close()


@pytest.mark.parametrize('failure', ['otm', 'stale', 'identity', 'incomplete', 'expiry'])
def test_existing_option_revalidation_keeps_eligibility_guards(failure):
    sample, rows, _ = example()
    config = ExecutionConfig()
    existing = select_option(sample, 'CALL', rows, config)
    later, quotes = move(sample, rows, 1, 96 if failure == 'otm' else 102.6)
    row = next(r for r in quotes if r['instrument_token'] == existing.instrument_token)
    if failure == 'stale': row['stale'] = True
    if failure == 'identity': row['trading_symbol'] = 'DIFFERENT'
    if failure == 'incomplete': quotes.pop()
    if failure == 'expiry': later.options['expiry_selection']['analysis_expiry'] = '2026-10-01'
    assert select_option(later, 'CALL', quotes, config, existing=existing) is None


@pytest.mark.parametrize('call', [True, False])
def test_early_setup_accepts_existing_itm_without_changing_contract(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        at = instant(sample.as_of)+timedelta(minutes=1)
        manager.observe_tick('NIFTY 50', 100.5 if call else 99.5, at, at)
        later, quotes = move(sample, rows, 181, 102.6 if call else 97.4)
        bars = ([minute_bar(at, open=100.9, high=101.6, low=100.8, close=101.5),
                 minute_bar(at+timedelta(minutes=1), open=101.5, high=102.7, low=101.4, close=102.6)] if call else
                [minute_bar(at, open=99.1, high=99.2, low=98.4, close=98.5),
                 minute_bar(at+timedelta(minutes=1), open=98.5, high=98.6, low=97.3, close=97.4)])
        early = manager.advance(record.signal_id, later, quotes, minute_bars=bars)
        assert early.state == 'EARLY_SETUP'
        assert early.plan.option == record.plan.option
        assert manager.journal.history(confirmed_only=True)['total'] == 0
    finally:
        manager.close()


def test_oct8_nifty_target_consumed_while_waiting_is_explicitly_missed():
    manager, sample, rows, record = put_setup('2026-10-08T11:14:34.575303+05:30',
        22445.7, 22460.15, 22419.55, 22444)
    try:
        later, quotes = move(sample, rows, 481.05, 22409.35)
        result = manager.advance(record.signal_id, later, quotes)
        assert result.state == 'INVALIDATED'
        assert 'First target reached before entry' in result.history[-1].reason
        assert not result.outcome
    finally:
        manager.close()


def test_oct8_retest_already_beyond_target_is_missed_not_a_new_breach():
    manager, sample, rows, record = put_setup('2026-10-08T11:25:49+05:30',
        22419.55, 22459, 22360.35, 22360.4)
    try:
        assert record.plan.entry_trigger.type == 'breakout_retest'
        at = instant(sample.as_of)+timedelta(seconds=1)
        saved = manager.observe_tick('NIFTY 50', 22358.85, at, at)
        assert not saved.trigger_watch.get('breached_at')
        later, quotes = move(sample, rows, 2, 22358.85)
        missed = manager.advance(record.signal_id, later, quotes)
        assert missed.state == 'INVALIDATED'
        assert 'First target reached before entry' in missed.history[-1].reason
        assert manager.journal.history(confirmed_only=True)['total'] == 0
    finally:
        manager.close()


def test_oct8_banknifty_waits_for_pullback_then_requires_fresh_retest():
    manager, sample, rows, record = put_setup('2026-10-08T14:50:05+05:30',
        54568.85, 54589.15, 54383.15, 54499, 'BANKNIFTY')
    try:
        later, quotes = move(sample, rows, 1, 54499)
        waiting = manager.advance(record.signal_id, later, quotes)
        assert waiting.state == 'CANDIDATE'
        assert waiting.entry_diagnostics['t1_rr'] == pytest.approx(1.28508042)
        assert 'pullback' in waiting.entry_diagnostics['reason']
        # Improving price alone cannot manufacture a new retest/breach.
        at = instant(sample.as_of)+timedelta(seconds=30)
        assert not manager.observe_tick('NIFTY BANK', 54560, at, at).trigger_watch.get('breached_at')
        manager.observe_tick('NIFTY BANK', 54570, at+timedelta(seconds=1), at+timedelta(seconds=1))
        breached = manager.observe_tick('NIFTY BANK', 54560, at+timedelta(seconds=2), at+timedelta(seconds=2))
        assert breached.trigger_watch['breached_at']
        later, quotes = move(sample, rows, 176, 54545)
        bars = [minute_bar('2026-10-08T14:51:00+05:30', symbol='NIFTY BANK',
                          open=54560, high=54561, low=54549, close=54550),
                minute_bar('2026-10-08T14:52:00+05:30', symbol='NIFTY BANK',
                          open=54550, high=54551, low=54544, close=54545)]
        result = manager.advance(record.signal_id, later, quotes, minute_bars=bars)
        assert result.state == 'EARLY_SETUP'
        assert result.plan == record.plan
    finally:
        manager.close()
