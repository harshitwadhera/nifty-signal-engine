from datetime import timedelta

import pytest

from app.signals.lifecycle import SignalLifecycle
from app.signals.planning import SignalPlanner
from app.signals.execution_models import instant
from test_execution import example, move, candle


def minute_bar(start, symbol='NIFTY 50', **changes):
    start = instant(start)
    base = dict(
        symbol=symbol,
        interval='1m',
        start_time=start.isoformat(),
        end_time=(start + timedelta(minutes=1)).isoformat(),
        completed=True,
        partial=False,
        open=99.9,
        high=100.8,
        low=99.8,
        close=100.5,
    )
    base.update(changes)
    return base


@pytest.mark.parametrize('call', [True, False])
def test_single_live_breach_never_confirms_or_creates_opportunity(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        price = 101 if call else 99
        direction = 'CALL' if call else 'PUT'
        crossed = manager.observe_tick('NIFTY 50', price,
            instant(sample.as_of) + timedelta(seconds=1),
            instant(sample.as_of) + timedelta(seconds=1))
        assert crossed.state == 'CANDIDATE'
        assert crossed.trigger_watch['direction'] == direction
        assert crossed.trigger_watch['breach_price'] == price
        assert manager.journal.history(confirmed_only=True)['total'] == 0
    finally:
        manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_completed_1m_continuation_creates_early_setup_but_not_confirmed_history(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        breach_at = instant(sample.as_of) + timedelta(minutes=1)
        manager.observe_tick('NIFTY 50', 101 if call else 99, breach_at, breach_at)
        newer, quotes = move(sample, rows, 181, 101.5 if call else 98.5)
        start = breach_at
        if call:
            bars = [
                minute_bar(start, open=99.9, high=100.8, low=99.8, close=100.5),
                minute_bar(start + timedelta(minutes=1), open=100.4, high=101.6, low=100.3, close=101.5),
            ]
        else:
            bars = [
                minute_bar(start, open=100.1, high=100.2, low=99.2, close=99.5),
                minute_bar(start + timedelta(minutes=1), open=99.6, high=99.7, low=98.4, close=98.5),
            ]
        early = manager.advance(record.signal_id, newer, quotes, minute_bars=bars)
        assert early.state == 'EARLY_SETUP'
        assert early.trigger_watch['structure_1m']['qualified'] is True
        assert early.trigger_watch['structure_1m']['direction'] == ('BULLISH' if call else 'BEARISH')
        assert [event.state for event in early.history][-1] == 'EARLY_SETUP'
        assert manager.journal.history(confirmed_only=True)['total'] == 0
    finally:
        manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_single_wick_or_one_minute_bar_is_not_early_setup(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        at = instant(sample.as_of) + timedelta(minutes=1)
        manager.observe_tick('NIFTY 50', 101 if call else 99, at, at)
        newer, quotes = move(sample, rows, 181, 99.9 if call else 100.1)
        start = at
        bar = (minute_bar(start, open=99.9, high=101, low=99.7, close=99.9) if call else
               minute_bar(start, open=100.1, high=100.3, low=99, close=100.1))
        result = manager.advance(record.signal_id, newer, quotes, minute_bars=[bar])
        assert result.state == 'CANDIDATE'
        assert result.trigger_watch == {}
        assert manager.journal.history(confirmed_only=True)['total'] == 0
    finally:
        manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_early_setup_failure_returns_to_wait_for_trigger(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        at = instant(sample.as_of) + timedelta(minutes=1)
        manager.observe_tick('NIFTY 50', 101 if call else 99, at, at)
        first, quotes = move(sample, rows, 181, 101.5 if call else 98.5)
        start = at
        if call:
            good = [
                minute_bar(start, close=100.5),
                minute_bar(start + timedelta(minutes=1), open=100.4, high=101.6, low=100.3, close=101.5),
            ]
        else:
            good = [
                minute_bar(start, open=100.1, high=100.2, low=99.2, close=99.5),
                minute_bar(start + timedelta(minutes=1), open=99.6, high=99.7, low=98.4, close=98.5),
            ]
        early = manager.advance(record.signal_id, first, quotes, minute_bars=good)
        assert early.state == 'EARLY_SETUP'
        later, quotes = move(first, quotes, 60, 99.8 if call else 100.2)
        failed = (minute_bar(start + timedelta(minutes=2), open=101.0, high=101.1, low=99.6, close=99.8) if call else
                  minute_bar(start + timedelta(minutes=2), open=99.0, high=100.4, low=98.9, close=100.2))
        result = manager.advance(record.signal_id, later, quotes, minute_bars=[*good, failed])
        assert result.state == 'CANDIDATE'
        assert result.trigger_watch == {}
        assert [event.state for event in result.history][-2:] == ['EARLY_SETUP', 'CANDIDATE']
        assert manager.journal.history(confirmed_only=True)['total'] == 0
    finally:
        manager.close()


@pytest.mark.parametrize('call', [True, False])
def test_existing_5m_confirmation_remains_only_confirmed_transition(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        at = instant(sample.as_of) + timedelta(minutes=1)
        manager.observe_tick('NIFTY 50', 101 if call else 99, at, at)
        early_snapshot, quotes = move(sample, rows, 181, 101.5 if call else 98.5)
        start = at
        bars = ([
            minute_bar(start, close=100.5),
            minute_bar(start + timedelta(minutes=1), open=100.4, high=101.6, low=100.3, close=101.5),
        ] if call else [
            minute_bar(start, open=100.1, high=100.2, low=99.2, close=99.5),
            minute_bar(start + timedelta(minutes=1), open=99.6, high=99.7, low=98.4, close=98.5),
        ])
        early = manager.advance(record.signal_id, early_snapshot, quotes, minute_bars=bars)
        assert early.state == 'EARLY_SETUP'
        confirmed_snapshot, quotes = move(early_snapshot, quotes, 179, 101 if call else 99)
        confirmed = manager.advance(record.signal_id, confirmed_snapshot, quotes,
            candle(confirmed_snapshot, call), bars)
        assert confirmed.state == 'CONFIRMED'
        assert [event.state for event in confirmed.history][-2:] == ['EARLY_SETUP', 'CONFIRMED']
        assert manager.journal.history(confirmed_only=True)['total'] == 1
    finally:
        manager.close()


def test_stale_out_of_order_and_duplicate_ticks_do_not_create_false_breaches():
    sample, rows, engine = example(True)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        created = instant(sample.as_of)
        assert manager.observe_tick('NIFTY 50', 101, created - timedelta(seconds=1), created) == record
        assert manager.observe_tick('NIFTY 50', 101, created + timedelta(seconds=1),
            created + timedelta(seconds=70)) == record
        # A newer non-breach event establishes the event-time watermark. An older
        # crossing arriving later must not create a false breach.
        assert manager.observe_tick('NIFTY 50', 99.9, created + timedelta(seconds=3),
            created + timedelta(seconds=3)).trigger_watch == {}
        assert manager.observe_tick('NIFTY 50', 101, created + timedelta(seconds=2),
            created + timedelta(seconds=4)).trigger_watch == {}
        accepted = manager.observe_tick('NIFTY 50', 101, created + timedelta(seconds=4),
            created + timedelta(seconds=4))
        duplicate = manager.observe_tick('NIFTY 50', 102, created + timedelta(seconds=4),
            created + timedelta(seconds=4))
        assert accepted.trigger_watch['breached_at'] == duplicate.trigger_watch['breached_at']
        assert len(duplicate.history) == 1
    finally:
        manager.close()


def test_early_setup_restart_persists_watch_state(tmp_path):
    sample, rows, engine = example(True)
    from app.signals.lifecycle import SignalJournal
    path = tmp_path / 'signals.sqlite3'
    manager = SignalLifecycle(SignalPlanner(engine), SignalJournal(path))
    record = manager.submit(sample, rows).record
    at = instant(sample.as_of) + timedelta(seconds=1)
    manager.observe_tick('NIFTY 50', 101, at, at)
    manager.close()
    restored = SignalLifecycle(SignalPlanner(engine), SignalJournal(path))
    try:
        saved = restored.records[record.signal_id]
        assert saved.state == 'CANDIDATE'
        assert saved.trigger_watch['breached_at'] == at.isoformat()
        assert saved.trigger_watch['selected_option'] == record.plan.option.trading_symbol
    finally:
        restored.close()


@pytest.mark.parametrize('call', [True, False])
def test_early_setup_is_demoted_when_current_direction_is_lost(call):
    sample, rows, engine = example(call)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        breach_at = instant(sample.as_of) + timedelta(minutes=1)
        manager.observe_tick('NIFTY 50', 101 if call else 99, breach_at, breach_at)
        early_snapshot, quotes = move(sample, rows, 181, 101.5 if call else 98.5)
        start = breach_at
        bars = ([
            minute_bar(start, close=100.5),
            minute_bar(start + timedelta(minutes=1), open=100.4, high=101.6, low=100.3, close=101.5),
        ] if call else [
            minute_bar(start, open=100.1, high=100.2, low=99.2, close=99.5),
            minute_bar(start + timedelta(minutes=1), open=99.6, high=99.7, low=98.4, close=98.5),
        ])
        early = manager.advance(record.signal_id, early_snapshot, quotes, minute_bars=bars)
        assert early.state == 'EARLY_SETUP'
        engine.decide.return_value.decision = 'PUT' if call else 'CALL'
        later, quotes = move(early_snapshot, rows, 60, 101.6 if call else 98.4)
        result = manager.advance(record.signal_id, later, quotes, minute_bars=bars)
        assert result.state == 'CANDIDATE'
        assert result.trigger_watch == {}
    finally:
        manager.close()


def test_breach_inside_existing_minute_candle_does_not_count_that_candle():
    sample, rows, engine = example(True)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        breach_at = instant(sample.as_of) + timedelta(seconds=30)
        manager.observe_tick('NIFTY 50', 101, breach_at, breach_at)
        later, quotes = move(sample, rows, 121, 101.5)
        bars = [
            minute_bar(instant(sample.as_of), close=100.5),
            minute_bar(instant(sample.as_of) + timedelta(minutes=1), open=100.4, high=101.6, low=100.3, close=101.5),
        ]
        result = manager.advance(record.signal_id, later, quotes, minute_bars=bars)
        assert result.state == 'CANDIDATE'
    finally:
        manager.close()
