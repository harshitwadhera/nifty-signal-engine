"""Read-only opportunity projections over the real signal and manual SQLite journals."""
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.signals.execution_models import LifecycleEvent
from app.signals.lifecycle import SignalJournal, SignalLifecycle
from app.signals.planning import SignalPlanner
from app.trades.storage import TradeJournal
from test_execution import example, confirm


@pytest.fixture
def history_api(tmp_path):
    sample, quotes, engine = example()
    manager = SignalLifecycle(SignalPlanner(engine))
    base, _, _ = confirm(manager, sample, quotes)
    manager.close()
    path = tmp_path/'opportunities.sqlite3'
    journal, trades = SignalJournal(path), TradeJournal(path)
    signals, manual = Mock(), Mock()
    signals.history.side_effect = journal.history
    manual.by_signal_ids.side_effect = trades.by_signal_ids
    with TestClient(create_app(Settings(), stream=Mock(), engine=Mock(), options=Mock(),
                               signals=signals, trades=manual)) as client:
        yield client, journal, trades, base, manual
    journal.close()
    trades.close()


def save(journal, base, signal_id, state='CONFIRMED', index='NIFTY', created='10:00', confirmed='10:05'):
    stamp = lambda t: f'2026-09-25T{t}:00+05:30'
    history = [LifecycleEvent('CANDIDATE', stamp(created), 'Created')]
    if state not in ('CANDIDATE', 'INVALIDATED', 'UNCONFIRMED_EXPIRED'):
        history.append(LifecycleEvent('CONFIRMED', stamp(confirmed), 'Observed confirmation'))
    state = 'EXPIRED' if state == 'UNCONFIRMED_EXPIRED' else state
    if state not in ('CANDIDATE', 'CONFIRMED'):
        history.append(LifecycleEvent(state, stamp('12:00'), 'Observed lifecycle'))
    record = replace(base, signal_id=signal_id, state=state, plan=replace(base.plan, index_name=index),
                     created_at=stamp(created), updated_at=history[-1].at, history=tuple(history))
    journal.save(record)
    return record


def get(client, query=''):
    response = client.get('/api/signals/opportunities'+query)
    assert response.status_code == 200, response.text
    return response.json()


def test_confirmed_lifecycle_taken_filters_pagination_restart_and_no_mutation(history_api):
    client, journal, trades, base, manual = history_api
    for state in ('CANDIDATE', 'INVALIDATED', 'UNCONFIRMED_EXPIRED', 'CONFIRMED',
                  'TARGET1_HIT', 'TARGET2_HIT', 'STOPPED', 'EXPIRED'):
        save(journal, base, state, state, index='BANKNIFTY' if state in ('STOPPED', 'EXPIRED') else 'NIFTY')
    for signal_id in ('STOPPED', 'unrelated-signal'):
        trades.save(dict(trade_id='manual-'+signal_id, signal_id=signal_id, index_name='BANKNIFTY',
                         status='USER_CLOSED', opened_at=base.created_at, closed_at=base.updated_at), create=True)
    before = list(journal.db.iterdump())
    queries = []
    trades.db.set_trace_callback(queries.append)
    data = get(client)
    assert data['total'] == 5
    items = {item['signal_id']: item for item in data['items']}
    assert set(items) == {'CONFIRMED', 'TARGET1_HIT', 'TARGET2_HIT', 'STOPPED', 'EXPIRED'}
    assert {key for key, item in items.items() if item['taken']} == {'STOPPED'}
    assert items['TARGET2_HIT']['state'] == 'TARGET2_HIT' and not items['TARGET2_HIT']['taken']
    assert items['STOPPED']['manual_trade']['status'] == 'USER_CLOSED'
    assert len([q for q in queries if q.startswith('SELECT')]) == 1
    manual.by_signal_ids.assert_called_once()
    assert all('actual_pnl' not in item and 'fill_price' not in item for item in items.values())
    assert get(client, '?index=nifty')['total'] == 3
    assert get(client, '?index=banknifty')['total'] == 2
    pages = [get(client, f'?limit=1&offset={i}')['items'][0]['signal_id'] for i in range(5)]
    assert pages == sorted(items, reverse=True)  # Tied confirmation times use signal_id.
    assert get(client, '?offset=5')['items'] == []
    assert list(journal.db.iterdump()) == before
    path = journal.db.execute('PRAGMA database_list').fetchone()[2]
    restarted, restarted_trades = SignalJournal(path), TradeJournal(path)
    try:
        assert restarted.history(confirmed_only=True) == journal.history(confirmed_only=True)
        assert set(restarted_trades.by_signal_ids(items)) == {'STOPPED'}
        assert list(journal.db.iterdump()) == before
    finally:
        restarted.close()
        restarted_trades.close()


def test_newest_confirmation_first_even_when_created_earlier(history_api):
    client, journal, _, base, _ = history_api
    save(journal, base, 'A', created='10:00', confirmed='10:20')
    save(journal, base, 'B', created='10:05', confirmed='10:10')
    save(journal, base, 'C', created='10:06', confirmed='10:20')
    save(journal, base, 'waiting', state='CANDIDATE', created='10:30')
    assert [item['signal_id'] for item in get(client)['items']] == ['C', 'A', 'B']
    for offset, expected in enumerate(['C', 'A', 'B']):
        page = get(client, f'?limit=1&offset={offset}')
        assert page['total'] == 3 and page['items'][0]['signal_id'] == expected
        assert get(client, f'?limit=1&offset={offset}') == page
    # The general signal audit retains its existing creation-time ordering.
    assert journal.history()['items'][0]['signal_id'] == 'waiting'


@pytest.mark.parametrize('malformed', ['missing', None, [None, 42, 'bad', {}], {}, 'bad'])
def test_incomplete_record_history_uses_persisted_events_without_mutation(history_api, malformed):
    client, journal, _, base, _ = history_api
    save(journal, base, 'old', state='TARGET2_HIT')
    row = journal.history()['items'][0]
    if malformed == 'missing':
        row.pop('history')
    else:
        row['history'] = malformed
    row['plan'] = None
    row.pop('confirmation_price')
    row['outcome'] = None
    with journal.db:
        journal.db.execute('UPDATE signals SET record=? WHERE signal_id=?', (json.dumps(row), 'old'))
    before = list(journal.db.iterdump())
    item = get(client)['items'][0]
    assert item['confirmed_at'] == '2026-09-25T10:05:00+05:30'
    assert [event['state'] for event in item['history']] == ['CANDIDATE', 'CONFIRMED', 'TARGET2_HIT']
    assert item['index_name'] == 'NIFTY' and item['direction'] == 'CALL'
    assert item['plan'] == {'index_name': 'NIFTY', 'direction': 'CALL'}
    assert item['confirmation_price'] is None and item['outcome'] == {} and not item['taken']
    assert list(journal.db.iterdump()) == before


@pytest.mark.parametrize('payload', ['null', '[]', 'broken JSON'])
def test_malformed_serialized_row_does_not_hide_other_opportunities(history_api, payload):
    client, journal, _, base, _ = history_api
    save(journal, base, 'bad')
    save(journal, base, 'good')
    with journal.db:
        journal.db.execute('UPDATE signals SET record=? WHERE signal_id=?', (payload, 'bad'))
    data = get(client)
    assert data['total'] == len(data['items']) == 2
    assert all(item['confirmed_at'] and item['signal_id'] for item in data['items'])


def test_missing_optional_plan_fields_and_legacy_migration(history_api):
    client, journal, _, base, _ = history_api
    save(journal, base, 'legacy')
    row = journal.history()['items'][0]
    row['plan'].pop('option')
    row['plan'].pop('target2')
    row.pop('confirmation_price')
    row['outcome'] = {}
    row['history'].insert(0, None)
    with journal.db:
        journal.db.execute('UPDATE signal_lifecycle SET payload=?', (json.dumps(row),))
        for table in ('signals', 'signal_events', 'signal_outcomes'):
            journal.db.execute('DROP TABLE '+table)
    migrated = SignalJournal(journal.db.execute('PRAGMA database_list').fetchone()[2])
    try:
        before = list(journal.db.iterdump())
        item = get(client)['items'][0]
        assert item['confirmed_at'] == '2026-09-25T10:05:00+05:30'
        assert item['plan'].get('option') is None and item['plan'].get('target2') is None
        assert item['confirmation_price'] is None
        assert list(journal.db.iterdump()) == before
    finally:
        migrated.close()


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'offset=-1', 'index=invalid'])
def test_opportunity_parameters_validated(history_api, query):
    assert history_api[0].get('/api/signals/opportunities?'+query).status_code == 422
