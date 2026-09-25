from datetime import date, datetime, timedelta
from unittest.mock import Mock
import json

import pytest

from app.analytics.session import IST
from app.breadth.metrics import calculate
from app.breadth.universe import ConstituentSource, resolve_constituents, MAJOR_BANKS
from app.breadth.service import BreadthService
from app.streaming.market_state import Tick
from test_streaming import feed, connect


def membership(symbols=('A', 'B', 'C')):
    return dict(symbols=list(symbols), weights={}, membership_valid=True, full_index=True, source='test')


def test_advances_declines_unchanged_and_ema_coverage():
    rows = {s: dict(last_price=p, previous_close=100, stale=False, **{'5m_ema20': 100})
            for s, p in [('A', 110), ('B', 90), ('C', 100)]}
    result = calculate('NIFTY', membership(), rows, 'test')
    assert (result['advances'], result['declines'], result['unchanged']) == (1, 1, 1)
    assert result['advance_decline_ratio'] == 1
    assert result['percent_positive'] == pytest.approx(100/3)
    assert result['percent_above_5m_ema20'] == pytest.approx(100/3)
    assert result['percent_above_15m_ema20'] is None
    assert result['percent_above_15m_ema20_coverage'] == 0
    assert result['weighting'] == 'unweighted'


def test_weights_and_partial_weight_fallback():
    members = membership(('A', 'B'))
    rows = {'A': dict(last_price=110, previous_close=100, stale=False),
            'B': dict(last_price=90, previous_close=100, stale=False)}
    members['weights'] = {'A': 80, 'B': 20}
    assert calculate('BANKNIFTY', members, rows, '')['percent_positive'] == 80
    members['weights'].pop('B')
    result = calculate('BANKNIFTY', members, rows, '')
    assert result['percent_positive'] == 50 and result['weighting'] == 'unweighted'


def test_missing_stale_nonfinite_and_zero_declines():
    rows = {'A': dict(last_price=110, previous_close=100, stale=False),
            'B': dict(last_price=float('inf'), previous_close=100, stale=False),
            'C': dict(last_price=90, previous_close=100, stale=True)}
    result = calculate('NIFTY', membership(), rows, '')
    assert result['coverage_percent'] == pytest.approx(100/3)
    assert result['advance_decline_ratio'] is None
    assert result['declines'] == result['unchanged'] == 0
    json.dumps(result, allow_nan=False)


def test_constituent_source_csv_and_failure_proxy():
    def fetch(url):
        symbols = [f'S{i}' for i in range(50)] if '50list' in url else MAJOR_BANKS
        return 'Symbol,Series\n' + '\n'.join(s+',EQ' for s in symbols)
    universe = ConstituentSource(fetch=fetch).load(date(2026, 9, 25))
    assert len(universe['NIFTY']['symbols']) == 50
    assert universe['BANKNIFTY']['full_index']
    failed = ConstituentSource(fetch=Mock(side_effect=RuntimeError())).load(date(2026, 9, 25))
    assert failed['BANKNIFTY']['symbols'] == list(MAJOR_BANKS)
    assert not failed['BANKNIFTY']['full_index'] and not failed['NIFTY']['symbols']


def test_dated_config_membership(tmp_path):
    path = tmp_path/'members.json'
    path.write_text(json.dumps({'BANKNIFTY': {'symbols': list(MAJOR_BANKS), 'as_of': '2026-09-25', 'full_index': False}}))
    source = ConstituentSource(path)
    assert source.load(date(2026, 9, 25))['BANKNIFTY']['membership_valid']
    assert not source.load(date(2026, 9, 26))['BANKNIFTY']['membership_valid']


def test_resolution_uses_metadata_and_preserves_missing_denominator():
    resolver = Mock()
    resolver.find.side_effect = lambda client, exchange, **kw: ([{'instrument_token': 5678}] if kw['tradingsymbol'] == 'HDFCBANK' else [])
    universe = {'BANKNIFTY': membership(MAJOR_BANKS)}
    result = resolve_constituents(resolver, Mock(), universe)
    assert result == [dict(instrument_token=5678, trading_symbol='HDFCBANK', friendly_name='HDFCBANK')]
    assert len(universe['BANKNIFTY']['symbols']) == 5


def test_breadth_uses_existing_connection_and_reconnects(feed):
    stream, session, _, transports, clock = feed
    ticker = connect(feed)
    sink = Mock()
    stream.set_breadth_consumer(sink)
    contracts = [dict(instrument_token=456, trading_symbol='HDFCBANK')]
    transports[0].update_subscriptions = Mock()
    stream.set_breadth_contracts(contracts)
    stream.set_breadth_contracts(contracts)
    transports[0].update_subscriptions.assert_called_once_with([456], [])
    ticker.on_ticks(ticker, [{'instrument_token': 456, 'last_price': 123, 'ohlc': {'close': 120}}])
    assert sink.call_args.args[0].symbol == 'HDFCBANK'
    assert len(stream.live()['instruments']) == 3
    ticker.on_connect(ticker, {})
    assert 456 in ticker.subscribe.call_args.args[0]
    stream.reconcile()
    assert len(transports) == 1
    stream.set_breadth_contracts([])


def test_service_reset_staleness_and_shutdown():
    clock = Mock(return_value=datetime(2026, 9, 25, 10, tzinfo=IST))
    stream, session, client = Mock(), Mock(), Mock()
    stream.state.clock = clock
    stream.status.return_value = {'websocket_status': 'connected'}
    stream.resolver.find.return_value = [{'instrument_token': 456}]
    session.get.return_value = 'dummy'
    source = Mock()
    source.load.return_value = {'BANKNIFTY': membership(('HDFCBANK',))}
    service = BreadthService(stream, session, lambda: client, source)
    try:
        service.reconcile()
        tick = Tick(456, 'HDFCBANK', 110, clock(), clock(), (('close', 100),))
        service.process(service.key, tick)
        assert service.snapshot('BANKNIFTY')['advances'] == 1
        assert service.snapshot('BANKNIFTY')['percent_above_5m_ema20'] is None
        clock.return_value += timedelta(seconds=31)
        assert service.snapshot('BANKNIFTY')['stale']
        session.get.return_value = None
        assert service.snapshot('BANKNIFTY')['stale']
        old_key = service.key
        service.reconcile()
        service.process(old_key, tick)
        assert not service.latest
    finally:
        service.shutdown()
        service.shutdown()
    stream.set_breadth_consumer.assert_called_with(None)


def test_ema_requires_twenty_complete_contiguous_bars():
    clock = Mock(return_value=datetime(2026, 9, 25, 14, 15, 6, tzinfo=IST))
    stream, session = Mock(), Mock()
    stream.state.clock = clock
    stream.status.return_value = {'websocket_status': 'connected'}
    session.get.return_value = 'dummy'
    service = BreadthService(stream, session, Mock())
    service.key = ('dummy', clock().date())
    service.universe = {'BANKNIFTY': membership(('HDFCBANK',))}
    service.latest['HDFCBANK'] = Tick(456, 'HDFCBANK', 110, clock(), clock(), (('close', 100),))
    bars = []
    for i in range(20):
        start = clock().replace(hour=9, minute=15, second=0)+timedelta(minutes=15*i)
        bars.append(dict(symbol='HDFCBANK', interval='15m', start_time=start.isoformat(),
                         end_time=(start+timedelta(minutes=15)).isoformat(), open=100, high=100,
                         low=100, close=100, completed=True, partial=False, source='live', volume=None))
    try:
        service.db.save(bars[1:])
        assert service.snapshot('BANKNIFTY')['percent_above_15m_ema20'] is None
        service.db.save(bars[:1])
        assert service.snapshot('BANKNIFTY')['percent_above_15m_ema20'] == 100
        with service.db.db:
            service.db.db.execute('DELETE FROM candles WHERE candle_start=?', (bars[10]['start_time'],))
        assert service.snapshot('BANKNIFTY')['percent_above_15m_ema20'] is None
    finally:
        service.shutdown()
