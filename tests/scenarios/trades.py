"""Drive the real manual TradeService/SQLite journal with isolated synthetic inputs."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from app.trades.models import Confirmation
from app.trades.service import TradeService
from app.trades.storage import TradeJournal
from .market import NOW, build


@contextmanager
def trade_session(direction='CALL', scenario=None):
    scenario = scenario or build('qualified_call' if direction == 'CALL' else 'qualified_put')
    snapshot = scenario.snapshot
    state = SimpleNamespace(now=NOW, connected=True)
    spot, sign = snapshot.structure['spot'], 1 if direction == 'CALL' else -1
    option = deepcopy(snapshot.options['atm_ce' if direction == 'CALL' else 'atm_pe'])
    row = dict(trading_symbol='NIFTY 50', last_price=spot, stale=False,
               last_tick_received_at=NOW.isoformat(), last_exchange_timestamp=NOW.isoformat())
    # Synthetic confirmed plan, not a claim that SignalPlanner confirmed this input.
    # Confirmation, setup validation, transitions and persistence below are real.
    record = dict(signal_id='SIM_MANUAL_'+direction, state='CONFIRMED',
                  updated_at=(NOW-timedelta(seconds=60)).isoformat(), confirmation_price=spot,
                  plan=dict(index_name='NIFTY', direction=direction,
                            entry_trigger=dict(instrument='NIFTY 50', level=spot, type='breakout'),
                            invalidation={'level': spot-sign*100}, target1={'level': spot+sign*200},
                            target2={'level': spot+sign*400}, option=option, t1_rr=2, t2_rr=4))
    signals = SimpleNamespace(
        current=lambda index: {'record': deepcopy(record), 'data_quality': {'stale': False}},
        detail=lambda sid: {'record': deepcopy(record)} if sid == record['signal_id'] else None)
    stream = SimpleNamespace(live=lambda: {'websocket_status': 'connected' if state.connected else 'disconnected',
                                          'instruments': [deepcopy(row)]})
    options = SimpleNamespace(response=lambda *args: ({}, [deepcopy(option)]))
    # No caller-supplied DB path and no MARKET_DB_PATH lookup, even for CLI runs.
    with TemporaryDirectory(prefix='nifty-local-scenario-') as temporary:
        path = Path(temporary) / 'trades.sqlite3'
        service = TradeService(stream, signals, options, path=path, clock=lambda: state.now)
        body = Confirmation(quantity=option['lot_size'], lots=1, actual_entry_premium=option['ltp'],
                            underlying_entry=spot, opened_at=NOW.isoformat())
        def confirm():
            # Random IDs are orthogonal to market logic; keep replay output deterministic.
            with patch('app.trades.service.uuid4', return_value=UUID(int=1)):
                return service.confirm(record['signal_id'], body)
        def observe(value, *, stale=False, received_age=0, exchange_age=0, seconds=1):
            state.now += timedelta(seconds=seconds)
            row.update(last_price=value, stale=stale,
                       last_tick_received_at=(state.now-timedelta(seconds=received_age)).isoformat(),
                       last_exchange_timestamp=(state.now-timedelta(seconds=exchange_age)).isoformat())
            service.poll()
        try:
            yield SimpleNamespace(service=service, state=state, row=row, option=option, record=record,
                                  confirm=confirm, observe=observe, path=path, spot=spot, sign=sign)
        finally:
            service.close()


TRADE_SCENARIOS = ('active_trade_stale_stop', 'active_trade_stale_targets', 'active_trade_stop',
                   'active_trade_stop_put', 'targets', 'targets_put', 'trade_out_of_order',
                   'trade_future_timestamp', 'trade_exchange_out_of_order')


def trade_report(name):
    if name not in TRADE_SCENARIOS:
        raise KeyError(name)
    with trade_session('PUT' if name.endswith('_put') else 'CALL') as h:
        trade = h.confirm()
        stop, t1, t2 = (trade[k] for k in ('underlying_stop', 'target1', 'target2'))
        if name == 'active_trade_stale_stop':
            h.observe(stop-h.sign, stale=True)
        elif name == 'active_trade_stale_targets':
            h.observe(t2+h.sign, received_age=31)
        elif name.startswith('active_trade_stop'):
            h.observe(stop-h.sign)
            h.observe(stop-h.sign)  # Same crossing on another fresh receipt, still one event.
        elif name.startswith('targets'):
            for value in (t1+h.sign, t1+h.sign, t2+h.sign, t2+h.sign):
                h.observe(value)
        elif name == 'trade_out_of_order':
            h.observe(h.spot)
            h.observe(stop-h.sign, received_age=2, exchange_age=2)
        elif name == 'trade_future_timestamp':
            h.observe(stop-h.sign, exchange_age=-1)
        elif name == 'trade_exchange_out_of_order':
            h.observe(h.spot)
            h.observe(stop-h.sign, exchange_age=2)  # Old exchange event, newer receipt.
        stored = h.service.journal.get(trade['trade_id'])
        events = h.service.journal.events(trade['trade_id'])
        # A second connection sees persisted state/events, not just mutated Python objects.
        reopened = TradeJournal(h.path)
        try:
            durable = reopened.get(trade['trade_id']) == stored and reopened.events(trade['trade_id']) == events
        finally:
            reopened.close()
        return {'scenario': name, 'kind': 'trade', 'as_of': h.state.now.isoformat(),
                'status': stored['status'], 'monitoring_status': stored['monitoring_status'],
                'closed_at': stored['closed_at'], 'hits': stored['metadata']['hits'], 'events': events,
                'persisted_on_reopen': durable,
                'notes': ['Synthetic confirmed plan; production setup/confirmation/monitor/journal; temporary SQLite only.']}
