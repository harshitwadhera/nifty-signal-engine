from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from kiteconnect.exceptions import TokenException

from app.config import Settings
from app.main import create_app
from app.session import SessionStore
from app.streaming.instruments import InstrumentResolver, INDEX_NAMES
from app.streaming.kite_stream import KiteStream
from app.streaming.market_state import MarketState, Tick
from app.streaming.transport import TickerTransport, ManagedTicker


class FakeTransport:
    def __init__(self, key, token):
        self.ticker = Mock()
        self.ticker.MODE_FULL = "full"
        self.start = Mock()
        self.close = Mock()


@pytest.fixture
def feed():
    clock = Mock(return_value=datetime(2026, 9, 24, 10, tzinfo=timezone.utc))
    session = SessionStore(clock)
    client = Mock()
    client.instruments.return_value = [
        {"exchange": "NSE", "tradingsymbol": name, "segment": "INDICES", "instrument_token": i}
        for i, name in enumerate(INDEX_NAMES, 101)]
    transports = []

    def factory(key, token):
        transport = FakeTransport(key, token)
        transports.append(transport)
        return transport

    stream = KiteStream(Settings("dummy-key", "dummy-secret"), session, lambda: client,
                        resolver=InstrumentResolver(clock), state=MarketState(clock), transport_factory=factory)
    yield stream, session, client, transports, clock
    stream.shutdown()


def connect(feed):
    stream, session, _, transports, _ = feed
    session.save("dummy-session")
    stream.reconcile()
    ticker = transports[-1].ticker
    ticker.on_connect(ticker, {})
    return ticker


def ticks(ticker):
    ticker.on_ticks(ticker, [{"instrument_token": i, "last_price": 100.5} for i in range(101, 104)])


def test_waits_for_authentication(feed):
    stream, _, client, transports, _ = feed
    stream.reconcile()
    assert not transports
    client.instruments.assert_not_called()
    assert stream.status()["status"] == "authentication_required"


def test_connect_and_subscribe_exact_indices(feed):
    ticker = connect(feed)
    ticker.subscribe.assert_called_once_with([101, 102, 103])
    ticker.set_mode.assert_called_once_with("full", [101, 102, 103])
    assert feed[0].status()["websocket_status"] == "connected"
    assert feed[0].status()["last_connected_at"] is not None


def test_ticks_and_optional_fields(feed):
    ticker = connect(feed)
    stream, _, _, _, clock = feed
    queue = stream.state.subscribe()
    ticker.on_ticks(ticker, [{"instrument_token": 101, "last_price": 123,
                            "ohlc": {"open": 120, "close": 119}, "exchange_timestamp": clock()}])
    row = stream.live()["instruments"][0]
    assert row["last_price"] == 123 and row["tick_count"] == 1
    assert row["ohlc"] == {"open": 120, "close": 119}
    assert row["last_exchange_timestamp"] == clock().isoformat()
    assert row["last_tick_received_at"] == clock().isoformat()
    event = queue.get_nowait()
    assert isinstance(event, Tick) and event.trading_symbol == "NIFTY 50"
    ticks(ticker)
    assert stream.live()["instruments"][1]["ohlc"] is None
    assert stream.live()["instruments"][1]["last_exchange_timestamp"] is None
    assert stream.live()["instruments"][0]["tick_count"] == 2
    assert stream.status()["status"] == "connected"


def test_stale_quiet_market_is_not_disconnection(feed):
    ticker = connect(feed)
    ticks(ticker)
    stream, _, _, _, clock = feed
    clock.return_value += timedelta(seconds=31)
    assert stream.status()["status"] == "stale"
    assert stream.status()["websocket_status"] == "connected"
    assert stream.status()["market_open"] is None
    assert all(row["stale"] for row in stream.live()["instruments"])
    ticks(ticker)
    assert stream.status()["status"] == "connected"


def test_disconnect_reconnect_resubscribe(feed):
    ticker = connect(feed)
    ticks(ticker)
    ticker.on_close(ticker, 1006, "network outage")
    assert feed[0].status()["status"] == "disconnected"
    assert feed[0].status()["last_disconnected_at"] is not None
    assert feed[0].live()["stale"]
    ticker.on_reconnect(ticker, 1)
    assert feed[0].status()["status"] == "reconnecting"
    ticker.on_connect(ticker, {})
    assert ticker.subscribe.call_count == 2
    assert len(feed[3]) == 1


def test_duplicate_connection_prevention_under_concurrency(feed):
    stream, session, _, transports, _ = feed
    session.save("dummy-session")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: stream.reconcile(), range(20)))
    assert len(transports) == 1
    transports[0].start.assert_called_once()


def test_new_login_closes_old_stream_and_ignores_old_callbacks(feed):
    old = connect(feed)
    stream, session, _, transports, _ = feed
    session.save("replacement-session")
    stream.reconcile()
    transports[0].close.assert_called_once()
    assert len(transports) == 2
    old.on_error(old, 403, "expired")
    ticks(old)
    assert session.get() == "replacement-session"
    assert all(row["tick_count"] == 0 for row in stream.live()["instruments"])


@pytest.mark.parametrize("code,reason", [(403, "denied"), (1006, "HTTP 403 forbidden"), (0, "TokenException"), (0, "Token is invalid or has expired")])
def test_expired_authentication(feed, code, reason):
    ticker = connect(feed)
    ticker.on_error(ticker, code, reason)
    stream, session, _, transports, _ = feed
    assert session.get() is None
    assert stream.status()["status"] == "authentication_required"
    transports[0].close.assert_called_once()
    ticker.on_reconnect(ticker, 1)
    stream.reconcile()
    assert len(transports) == 1


def test_expiry_without_browser_requests(feed):
    connect(feed)
    stream, session, _, transports, clock = feed
    clock.return_value += timedelta(days=1)
    stream.reconcile()
    assert session.get() is None
    transports[0].close.assert_called_once()
    assert stream.status()["status"] == "authentication_required"


def test_shutdown_and_late_callback(feed):
    ticker = connect(feed)
    stream, _, _, transports, _ = feed
    stream.shutdown()
    ticker.on_connect(ticker, {})
    ticks(ticker)
    stream.reconcile()
    assert len(transports) == 1
    transports[0].close.assert_called_once()
    assert stream.status()["status"] == "disconnected"
    assert all(row["tick_count"] == 0 for row in stream.live()["instruments"])


def test_retries_exhausted_do_not_create_another_connection(feed):
    ticker = connect(feed)
    ticker.on_noreconnect(ticker)
    for _ in range(10):
        feed[0].reconcile()
    assert len(feed[3]) == 1
    assert feed[0].status()["status"] == "disconnected"


def test_metadata_failure_bounded_retries(feed, monkeypatch):
    stream, session, client, transports, _ = feed
    session.save("dummy-session")
    client.instruments.side_effect = RuntimeError("sensitive-upstream-body")
    for n in range(10):
        monkeypatch.setattr("app.streaming.kite_stream.time.monotonic", lambda: n * 100)
        stream.reconcile()
    assert client.instruments.call_count == 3
    assert not transports
    assert stream.status()["status"] == "disconnected"


def test_metadata_token_failure_clears_session(feed):
    stream, session, client, _, _ = feed
    session.save("dummy-session")
    client.instruments.side_effect = TokenException("private")
    stream.reconcile()
    assert session.get() is None


def test_metadata_cache_and_rollover(feed):
    stream, _, client, _, clock = feed
    resolver = stream.resolver
    resolver.indices(client)
    resolver.indices(client)
    client.instruments.assert_called_once_with("NSE")
    clock.return_value += timedelta(days=1)
    resolver.indices(client)
    assert client.instruments.call_count == 2


def test_missing_and_ambiguous_metadata(feed):
    _, _, client, _, clock = feed
    client.instruments.return_value = []
    with pytest.raises(ValueError):
        InstrumentResolver(clock).indices(client)
    client.instruments.return_value = [{"exchange": "NSE", "tradingsymbol": "NIFTY 50", "segment": "INDICES", "instrument_token": 1}] * 2
    with pytest.raises(ValueError):
        InstrumentResolver(clock).indices(client)


def test_state_copy_isolation_and_bounded_events(feed):
    ticker = connect(feed)
    stream = feed[0]
    queue = stream.state.subscribe(maxsize=1)
    ticks(ticker)
    assert stream.state.dropped_events == 2
    snapshot = stream.live()
    snapshot["instruments"][0]["last_price"] = -1
    assert stream.live()["instruments"][0]["last_price"] == 100.5
    stream.state.unsubscribe(queue)


def test_invalid_ticks_ignored(feed):
    ticker = connect(feed)
    ticker.on_ticks(ticker, [None, {"instrument_token": 101, "last_price": float("nan")},
                            {"instrument_token": 101, "last_price": True},
                            {"instrument_token": 999, "last_price": 123}])
    assert all(row["tick_count"] == 0 for row in feed[0].live()["instruments"])


def test_concurrent_tick_state_updates(feed):
    ticker = connect(feed)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: ticks(ticker), range(50)))
    assert all(row["tick_count"] == 50 for row in feed[0].live()["instruments"])


def test_api_and_lifespan(feed):
    stream, session, client, _, _ = feed
    ticker = connect(feed)
    ticks(ticker)
    with TestClient(create_app(Settings("dummy-key", "dummy-secret"), lambda: client, session, stream=stream)) as browser:
        live = browser.get("/api/market/live")
        assert live.status_code == 200
        assert len(live.json()["instruments"]) == 3
        assert browser.get("/api/stream/status").json()["status"] == "connected"
        for sensitive in ("dummy-secret", "dummy-session", "wss://", "authorization"):
            assert sensitive not in live.text
    assert stream._stop.is_set()
    assert not stream._thread.is_alive()


def test_transport_bounds_and_shutdown_during_handshake(monkeypatch):
    monkeypatch.setattr("app.streaming.transport.dispatch", lambda fn: fn())
    transport = TickerTransport("dummy-key", "dummy-session")
    assert transport.ticker.reconnect_max_tries == 5
    assert transport.ticker.reconnect_max_delay == 30
    transport.ticker._create_connection(transport.ticker.socket_url)
    connector = Mock()
    transport.ticker.factory.startedConnecting(connector)
    transport.close()
    connector.disconnect.assert_called_once()
    assert not transport.ticker.factory.continueTrying


def test_sdk_raw_error_logs_are_suppressed(feed, capsys):
    from app.logging_config import configure_logging
    configure_logging()
    ticker = ManagedTicker("dummy-key", "dummy-session")
    ticker._on_error(None, 1006, "wss://private-url?access_token=sensitive")
    assert "sensitive" not in capsys.readouterr().err
