from dataclasses import asdict
from datetime import timedelta
from unittest.mock import Mock
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.config import Settings
from app.signals import SignalEngine, SignalConfig
from app.signals.lifecycle import SignalJournal, SignalLifecycle
from app.signals.planning import SignalPlanner
from app.signals.live import LiveSignals
from app.signals.execution_models import instant, ExecutionConfig
from test_decisions import ready
from test_execution import example, move, candle, confirm


@pytest.fixture
def tracking(tmp_path):
    sample, rows, engine = example()
    engine.config = SignalConfig()
    engine.decide.return_value = SignalEngine().decide(ready())
    journal = SignalJournal(tmp_path/'signals.sqlite3')
    manager = SignalLifecycle(SignalPlanner(engine), journal)
    yield manager, sample, rows
    manager.close()


def test_creation_evidence_is_immutable_and_complete(tracking):
    manager, sample, rows = tracking
    record = manager.submit(sample, rows).record
    original = manager.journal.detail(record.signal_id)['creation_explanation']
    assert original['input']['structure'] == sample.structure
    assert original['input']['options'] == sample.options
    assert original['input']['volatility'] == sample.volatility
    assert original['input']['breadth'] == sample.breadth
    assert original['scores']['confidence'] == 90
    assert original['scores']['category_scores']['price_trend']['bullish_points'] == 30
    assert original['execution_config']['minimum_t1_rr'] == 1.5
    assert original['scoring_config']['minimum_score'] == 70
    assert original['option_chain_context']
    newer, quotes = move(sample, rows, 300, 101)
    manager.advance(record.signal_id, newer, quotes, candle(newer))
    detail = manager.journal.detail(record.signal_id)
    assert detail['creation_explanation'] == original
    assert [e['event']['state'] for e in detail['events']] == ['CANDIDATE', 'CONFIRMED']
    assert detail['events'][1]['explanation']['input']['structure']['spot'] == 101
    assert detail['events'][0]['record']['plan']['entry_trigger']['level'] == 100
    assert instant(detail['creation_explanation']['as_of']) < instant(detail['events'][1]['event']['at'])


def test_outcome_targets_excursions_r_and_duration(tracking):
    manager, sample, rows = tracking
    record, sample, rows = confirm(manager, sample, rows)
    assert record.outcome['entry_option_ltp'] == 10
    assert record.outcome['entry_underlying'] == 101
    newer, quotes = move(sample, rows, 10, 110)
    record = manager.advance(record.signal_id, newer, quotes)
    assert record.outcome['t1_hit'] and record.outcome['time_to_t1_seconds'] == 10
    newer, quotes = move(newer, quotes, 10, 120)
    record = manager.advance(record.signal_id, newer, quotes)
    assert record.outcome['t2_hit'] and record.outcome['time_to_t2_seconds'] == 20
    assert record.outcome['duration_seconds'] == 20
    assert record.outcome['mfe'] == 19 and record.outcome['mae'] == 0
    assert record.outcome['result_r'] == 9.5
    assert not record.outcome['observation_gap']
    persisted = json.loads(manager.journal.db.execute('SELECT payload FROM signal_outcomes').fetchone()[0])
    assert persisted == record.outcome
    assert manager.journal.db.execute('SELECT count(*) FROM signal_events').fetchone()[0] == 4


def test_stop_gap_and_stale_expiry_outcomes(tracking):
    manager, sample, rows = tracking
    record, sample, rows = confirm(manager, sample, rows)
    newer, quotes = move(sample, rows, 40, 98)
    record = manager.advance(record.signal_id, newer, quotes)
    assert record.state == 'STOPPED'
    assert record.outcome['mae'] == 3 and record.outcome['stop_hit']
    assert record.outcome['time_to_stop_seconds'] == 40
    assert record.outcome['result_r'] == -1.5
    assert record.outcome['observation_gap']


def test_no_trade_is_not_written_every_cycle(tracking):
    manager, sample, rows = tracking
    manager.planner.engine.decide.return_value = SignalEngine().decide(ready().__class__('NIFTY', sample.as_of))
    for _ in range(20):
        assert manager.submit(sample, rows).record is None
    for table in ('signals', 'signal_events', 'signal_outcomes', 'signal_lifecycle'):
        assert manager.journal.db.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


def test_secret_and_nonfinite_context_guard(tracking):
    manager, sample, rows = tracking
    sample.structure.update(api_secret='DO-NOT-SAVE', access_token='DO-NOT-SAVE', arbitrary=float('inf'))
    record = manager.submit(sample, rows).record
    payload = manager.journal.db.execute('SELECT payload FROM signal_events').fetchone()[0]
    assert 'DO-NOT-SAVE' not in payload and 'Infinity' not in payload
    assert manager.journal.detail(record.signal_id)['creation_explanation']['input']['structure']['arbitrary'] is None


def test_history_filters_pagination_and_detail(tracking):
    manager, sample, rows = tracking
    record = manager.submit(sample, rows).record
    assert manager.journal.history(index='NIFTY', direction='CALL', state='CANDIDATE')['total'] == 1
    assert manager.journal.history(index='BANKNIFTY')['total'] == 0
    assert manager.journal.history(date_from='2026-09-26')['total'] == 0
    assert manager.journal.history(limit=1, offset=1)['items'] == []
    assert manager.journal.detail('unknown') is None
    assert manager.journal.detail(record.signal_id)['record']['state'] == 'CANDIDATE'


def test_signal_apis_are_read_only_and_validate_filters(tracking):
    manager, sample, rows = tracking
    record = manager.submit(sample, rows).record
    service = Mock()
    service.current.side_effect = lambda index: {'index': index, 'decision': 'NO_TRADE', 'state': None}
    service.history.side_effect = manager.journal.history
    service.detail.side_effect = manager.journal.detail
    app = create_app(Settings(), stream=Mock(), engine=Mock(), options=Mock(), signals=service)
    with TestClient(app) as browser:
        assert browser.get('/api/signals/nifty').json()['decision'] == 'NO_TRADE'
        assert browser.get('/api/signals/banknifty').json()['index'] == 'BANKNIFTY'
        assert len(browser.get('/api/signals/current').json()['signals']) == 2
        assert browser.get('/api/signals/history?index=nifty&state=CANDIDATE&direction=CALL&limit=1').json()['total'] == 1
        assert browser.get('/api/signals/'+record.signal_id).json()['creation_explanation']
        assert browser.get('/api/signals/unknown').status_code == 404
        for query in ('limit=0', 'limit=101', 'offset=-1', 'state=bad', 'index=bad', 'date_from=2026-10-01&date_to=2026-09-01'):
            assert browser.get('/api/signals/history?'+query).status_code == 422
        assert browser.post('/api/signals/nifty').status_code == 405
        service.evaluate.assert_not_called()
    service.start.assert_called_once()
    service.close.assert_called_once()


def test_background_observer_and_freshness_fallback():
    stream = Mock()
    stream.state.clock.return_value = instant('2026-09-25T10:00:00+05:30')
    live = LiveSignals(stream, Mock(), Mock(), Mock(), execution_config=ExecutionConfig())
    calls = []
    def evaluate(index):
        calls.append(index)
        if index == 'BANKNIFTY':
            live.stop.set()
    live.evaluate = evaluate
    live.start()
    live.start()
    live.thread.join(timeout=3)
    assert calls == ['NIFTY', 'BANKNIFTY']
    assert live.current('NIFTY')['decision'] == 'NO_TRADE'
    live.views['NIFTY'] = {'decision': 'CALL', 'last_updated': '2026-09-25T09:59:00+05:30'}
    assert live.current('NIFTY')['decision'] == 'NO_TRADE'
    live.close()


def test_restart_preserves_events_and_outcomes(tracking):
    manager, sample, rows = tracking
    record, sample, rows = confirm(manager, sample, rows)
    newer, quotes = move(sample, rows, 10, 110)
    record = manager.advance(record.signal_id, newer, quotes)
    path = manager.journal.db.execute('PRAGMA database_list').fetchone()[2]
    second = SignalLifecycle(manager.planner, SignalJournal(path))
    try:
        restored = second.records[record.signal_id]
        assert restored.outcome == record.outcome
        detail = second.journal.detail(record.signal_id)
        assert len(detail['events']) == 3
        assert detail['creation_explanation']['input']['structure']['spot'] == 99.9
    finally:
        second.close()


def test_whole_observation_rolls_back_on_write_failure(tracking, monkeypatch):
    manager, sample, rows = tracking
    record = manager.submit(sample, rows).record
    original_save = manager.journal.save
    def fail(record, context=None):
        original_save(record, context)
        if record.state == 'CONFIRMED':
            raise RuntimeError('simulated failure before commit')
    monkeypatch.setattr(manager.journal, 'save', fail)
    newer, quotes = move(sample, rows, 300, 101)
    with pytest.raises(RuntimeError):
        manager.advance(record.signal_id, newer, quotes, candle(newer))
    assert manager.records[record.signal_id] == record
    detail = manager.journal.detail(record.signal_id)
    assert detail['record']['state'] == 'CANDIDATE'
    assert len(detail['events']) == 1
    assert detail['record']['updated_at'] == record.updated_at


def test_legacy_migration_does_not_invent_explanations(tracking):
    manager, sample, rows = tracking
    record, sample, rows = confirm(manager, sample, rows)
    path = manager.journal.db.execute('PRAGMA database_list').fetchone()[2]
    with manager.journal.db:
        for table in ('signals', 'signal_events', 'signal_outcomes'):
            manager.journal.db.execute('DROP TABLE '+table)
    migrated = SignalJournal(path)
    try:
        detail = migrated.detail(record.signal_id)
        assert [e['event']['state'] for e in detail['events']] == ['CANDIDATE', 'CONFIRMED']
        assert all(e['explanation'] is None for e in detail['events'])
    finally:
        migrated.close()


def test_put_outcomes_are_direction_normalized():
    sample, rows, engine = example(False)
    manager = SignalLifecycle(SignalPlanner(engine))
    try:
        record = manager.submit(sample, rows).record
        newer, quotes = move(sample, rows, 300, 99)
        record = manager.advance(record.signal_id, newer, quotes, candle(newer, False))
        newer, quotes = move(newer, quotes, 10, 80)
        record = manager.advance(record.signal_id, newer, quotes)
        assert record.outcome['entry_underlying'] == 99
        assert record.outcome['mfe'] == 19 and record.outcome['mae'] == 0
        assert record.outcome['result_r'] == 9.5
    finally:
        manager.close()


def test_stale_observations_never_award_outcomes(tracking):
    manager, sample, rows = tracking
    record, sample, rows = confirm(manager, sample, rows)
    newer, quotes = move(sample, rows, 10, 120)
    newer.structure['stale'] = True
    record = manager.advance(record.signal_id, newer, quotes)
    assert record.state == 'CONFIRMED'
    assert record.outcome['mfe'] == 0 and not record.outcome['t1_hit']
    newer, quotes = move(newer, quotes, 24*3600, 120)
    record = manager.advance(record.signal_id, newer, quotes)
    assert record.state == 'EXPIRED'
    assert record.outcome['result_r'] is None and record.outcome['observation_gap']
