from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.analytics.candles import CandleAggregator
from app.analytics.engine import MarketEngine
from app.analytics.history import HistoryRecovery
from app.analytics.indicators import VWAP, ema, momentum
from app.analytics.session import IST, bounds
from app.analytics.storage import CandleStore
from app.config import Settings
from app.main import create_app
from app.session import SessionStore
from app.streaming.instruments import InstrumentResolver, INDEX_NAMES
from app.streaming.kite_stream import KiteStream
from app.streaming.market_state import MarketState, Tick


def at(hour=9, minute=15, second=0, day=25):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=IST)


def tick(timestamp=None, price=100, volume=None, symbol="NIFTY 50", token=1, received=None, average=None):
    timestamp = timestamp or at()
    return Tick(token, symbol, price, received or timestamp, timestamp, volume=volume, average_traded_price=average)


def minutes(start=None, count=30):
    start = start or at()
    return [{"date": start + timedelta(minutes=i), "open": 100+i, "high": 102+i,
             "low": 99+i, "close": 101+i, "volume": 10} for i in range(count)]


@pytest.fixture
def store(tmp_path):
    db = CandleStore(tmp_path / "candles.sqlite3")
    yield db
    db.close()


@pytest.mark.parametrize("interval,size", [("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30)])
def test_candle_intervals(store, interval, size):
    agg = CandleAggregator(store)
    for i in range(size):
        agg.consume(tick(at() + timedelta(minutes=i), 100 + i))
        agg.consume(tick(at() + timedelta(minutes=i, seconds=30), 90 + i))
    agg.advance(at() + timedelta(minutes=size, seconds=6))
    c = store.load("NIFTY 50", interval)[0]
    assert c["open"] == 100 and c["high"] == 100 + size - 1
    assert c["low"] == 90 and c["close"] == 90 + size - 1
    assert c["tick_count"] == size * 2
    assert c["completed"] and not c["partial"]
    assert c["volume"] is None


def test_boundary_and_no_empty_candles(store):
    agg = CandleAggregator(store)
    agg.consume(tick(at(9, 15, 59), 100))
    agg.consume(tick(at(9, 16), 101))
    agg.advance(at(9, 20))
    rows = store.load("NIFTY 50", "1m")
    assert len(rows) == 2
    assert rows[0]["end_time"] == rows[1]["start_time"]
    assert rows[0]["close"] == 100 and rows[1]["open"] == 101


def test_late_duplicate_and_out_of_order(store):
    agg = CandleAggregator(store)
    t = tick(at(9, 15, 3), 103)
    assert agg.consume(t)
    assert not agg.consume(t)
    assert agg.consume(tick(at(9, 15, 1), 101, received=at(9, 15, 4)))
    agg.advance(at(9, 16, 6))
    row = store.load("NIFTY 50", "1m")[0]
    assert (row["open"], row["close"], row["tick_count"]) == (101, 103, 2)
    assert not agg.consume(tick(at(9, 15, 59), 500, received=at(9, 16, 7)))
    assert store.load("NIFTY 50", "1m")[0] == row


def test_missing_minutes_and_startup_are_partial(store):
    agg = CandleAggregator(store)
    agg.consume(tick(at(9, 17, 30)))
    agg.advance(at(9, 20, 6))
    assert store.load("NIFTY 50", "5m")[0]["partial"]


def test_timezone_and_session_alignment():
    from datetime import timezone
    start, end = bounds(at(9, 44).astimezone(timezone.utc), "30m")
    assert start == at() and end == at(9, 45)
    assert bounds(at(15, 20), "30m")[1] == at(15, 30)
    assert bounds(at(15, 30), "1m") is None
    assert bounds(at(day=26), "1m") is None  # Saturday
    with pytest.raises(ValueError):
        bounds(datetime(2026, 9, 25), "1m")


def metadata(day=date(2026, 9, 25)):
    index = [{"exchange": "NSE", "tradingsymbol": name, "segment": "INDICES", "instrument_token": i}
             for i, name in enumerate(INDEX_NAMES, 1)]
    futures = [{"exchange": "NFO", "tradingsymbol": f"{name}_{offset}", "segment": "NFO-FUT", "instrument_type": "FUT",
                "name": name, "expiry": day + timedelta(days=offset), "instrument_token": i * 100 + offset}
               for i, name in enumerate(("NIFTY", "BANKNIFTY"), 1) for offset in (-1, 0, 30)]
    client = Mock()
    client.instruments.side_effect = lambda exchange: index if exchange == "NSE" else futures
    return client


def test_dynamic_futures_discovery_and_rollover():
    clock = Mock(return_value=at(10))
    resolver, client = InstrumentResolver(clock), metadata()
    universe = resolver.universe(client)
    assert len(universe) == 5
    assert [r["trading_symbol"] for r in universe[-2:]] == ["NIFTY_0", "BANKNIFTY_0"]
    clock.return_value = at(15, 30)
    assert [r["trading_symbol"] for r in resolver.universe(client)[-2:]] == ["NIFTY_30", "BANKNIFTY_30"]
    assert client.instruments.call_count == 2


def test_future_rollover_replaces_stream():
    clock = Mock(return_value=at(15, 29))
    session = SessionStore(clock)
    session.save("test-session")
    transports = []
    def factory(*args):
        transport = Mock()
        transports.append(transport)
        return transport
    stream = KiteStream(Settings("test", "test"), session, lambda: metadata(),
                        resolver=InstrumentResolver(clock), state=MarketState(clock), transport_factory=factory)
    stream.include_futures = True
    stream.reconcile()
    assert len(stream.live()["instruments"]) == 5
    clock.return_value = at(15, 30)
    stream.reconcile()
    transports[0].close.assert_called_once()
    assert len(transports) == 2
    assert stream.live()["instruments"][-1]["trading_symbol"] == "BANKNIFTY_30"
    stream.shutdown()


def test_vwap_cumulative_deltas():
    vwap = VWAP()
    vwap.update(tick(at(), volume=100))  # Baseline, not 100 trades at last price.
    assert vwap.value is None
    vwap.update(tick(at(9, 15, 1), price=110, volume=110))
    vwap.update(tick(at(9, 15, 2), price=120, volume=130))
    assert vwap.value == pytest.approx((110 * 10 + 120 * 20) / 30)
    assert not vwap.snapshot(120)["full_session"]
    vwap.update(tick(at(9, 15, 3), price=900, volume=130))
    assert vwap.value < 120


def test_vwap_exchange_average_and_resets():
    vwap = VWAP()
    vwap.update(tick(volume=1000, average=105))
    assert vwap.snapshot(110)["vwap"] == 105
    assert vwap.snapshot(110)["full_session"]
    vwap.update(tick(at(9, 15, 1), volume=0))
    assert vwap.value is None
    vwap.update(tick(at(9, 15, 2), volume=None))
    assert vwap.value is None
    vwap.update(tick(at(day=28), volume=20))
    assert vwap.value is None


def test_live_volume_does_not_count_entire_cumulative_volume(store):
    agg = CandleAggregator(store)
    agg.consume(tick(volume=1000))
    agg.consume(tick(at(9, 15, 1), volume=1010))
    agg.advance(at(9, 16, 6))
    assert store.load("NIFTY 50", "1m")[0]["volume"] is None


def test_ema_sma_seed_and_warmup():
    assert ema([1] * 8, 9) is None
    assert ema(list(range(1, 10)), 9) == 5
    assert ema(list(range(1, 11)), 9) == 6
    assert ema([100] * 40, 20) == pytest.approx(100)
    assert momentum([])["ema9_above_ema20"] is None


def test_sqlite_completed_uniqueness_and_recovery(store):
    agg = CandleAggregator(store)
    agg.import_minutes("NIFTY 50", minutes(), at(10))
    agg.import_minutes("NIFTY 50", minutes(), at(10))
    assert len(store.load("NIFTY 50", "1m")) == 30
    recovered = CandleAggregator(store)
    assert len(recovered.candles("NIFTY 50", "5m")) == 6
    c = recovered.candles("NIFTY 50", "5m")[0]
    assert c["tick_count"] is None and c["volume"] is None
    store.save([{**c, "start_time": at(11).isoformat(), "completed": False}])
    assert len(store.load("NIFTY 50", "5m")) == 6


def test_restart_database_reopen(tmp_path):
    path = tmp_path / "restart.sqlite3"
    first = CandleStore(path)
    CandleAggregator(first).import_minutes("NIFTY 50", minutes(), at(10))
    first.close()
    second = CandleStore(path)
    assert len(CandleAggregator(second).candles("NIFTY 50", "1m")) == 30
    second.close()


def test_history_repairs_partial_and_seeds_current(store):
    agg = CandleAggregator(store)
    agg.consume(tick(at(9, 22, 30), 300))
    agg.import_minutes("NIFTY 50", minutes(count=7), at(9, 22, 35))
    current = agg.candles("NIFTY 50", "15m")[-1]
    assert current["open"] == 100 and current["high"] == 300
    assert not current["completed"]
    agg.import_minutes("NIFTY 50", minutes(count=15), at(9, 30, 6))
    c = store.load("NIFTY 50", "15m")[0]
    assert not c["partial"] and c["source"] == "historical"


def test_partial_history_cannot_downgrade_full_candle(store):
    agg = CandleAggregator(store)
    agg.import_minutes("NIFTY 50", minutes(), at(10))
    original = store.load("NIFTY 50", "30m")[0]
    agg.import_minutes("NIFTY 50", minutes(at(9, 30), 15), at(10))
    assert store.load("NIFTY 50", "30m")[0] == original


def test_previous_session_uses_actual_bars_and_cache(store):
    agg, client = CandleAggregator(store), Mock()
    recovery = HistoryRecovery(store, agg)
    daily = [{"date": at(day=21), "high": 130, "low": 90, "close": 120}]
    client.historical_data.side_effect = lambda *args: daily if args[3] == "day" else []
    instrument = {"trading_symbol": "NIFTY 50", "instrument_token": 1}
    first = recovery.recover(client, instrument, at(day=25))
    second = recovery.recover(client, instrument, at(10, day=25))
    assert first == second and first["session_date"] == "2026-09-21"
    assert sum(call.args[3] == "day" for call in client.historical_data.call_args_list) == 1


@pytest.fixture
def engine(tmp_path):
    clock = Mock(return_value=at(9, 31))
    state, session, client = MarketState(clock), SessionStore(clock), Mock()
    state.reset([{"instrument_token": 1, "trading_symbol": "NIFTY 50", "friendly_name": "NIFTY"},
                 {"instrument_token": 2, "trading_symbol": "NIFTY_BANK", "friendly_name": "BANK"},
                 {"instrument_token": 3, "trading_symbol": "NIFTY_TEST_FUT", "friendly_name": "FUT", "alias": "NIFTY_FUT"}])
    session.save("dummy")
    value = MarketEngine(state, session, lambda: client, tmp_path / "engine.sqlite3", clock)
    yield value, clock, client
    value.shutdown()


def test_opening_range_structure_and_futures_vwap(engine):
    value, clock, _ = engine
    value.aggregator.import_minutes("NIFTY 50", minutes(count=15), clock())
    value.process(tick(clock(), price=130))
    value.process(tick(clock(), price=132, volume=1000, average=120, symbol="NIFTY_TEST_FUT", token=3))
    data = value.structure()["nifty"]
    assert data["opening_range_high"] == 116 and data["opening_range_low"] == 99
    assert data["opening_range_state"] == "above"
    assert data["futures_basis"] == 2 and data["futures_vwap"] == 120
    assert data["price_vs_vwap"] == "above"


def test_incomplete_opening_range_and_spot_volume_never_used(engine):
    value, clock, _ = engine
    value.aggregator.import_minutes("NIFTY 50", minutes(count=14), clock())
    value.process(tick(clock(), volume=1000, average=100))
    data = value.structure()["nifty"]
    assert data["opening_range_high"] is None
    assert data["futures_vwap"] is None


def test_new_endpoints_validation(engine):
    value, clock, _ = engine
    stream = Mock()
    stream.state = value.state
    # Inject lifecycle mocks: this test reads deterministic engine state only.
    wrapper = Mock(wraps=value)
    wrapper.start = Mock()
    wrapper.shutdown = Mock()
    with TestClient(create_app(Settings(), stream=stream, engine=wrapper)) as client:
        assert client.get("/api/market/structure").status_code == 200
        assert client.get("/api/candles/NIFTY?interval=5m&limit=100").status_code == 200
        for suffix in ("interval=2m", "limit=0", "limit=1001", "limit=abc"):
            assert client.get("/api/candles/NIFTY?" + suffix).status_code == 422
        assert client.get("/api/candles/unknown").status_code == 404
        assert client.get("/api/candles/NIFTY_FUT").json()["symbol"] == "NIFTY_TEST_FUT"


def test_internal_tick_normalization():
    clock = Mock(return_value=at(10))
    session = SessionStore(clock)
    session.save("dummy")
    transport = Mock()
    stream = KiteStream(Settings("dummy", "dummy"), session, lambda: metadata(),
                        resolver=InstrumentResolver(clock), state=MarketState(clock), transport_factory=lambda *args: transport)
    stream.include_futures = True
    stream.reconcile()
    queue = stream.state.subscribe()
    transport.ticker.on_ticks(transport.ticker, [{"instrument_token": 100, "last_price": 200,
        "volume_traded": 1000, "oi": 4500, "average_traded_price": 195, "exchange_timestamp": clock()}])
    normalized = queue.get_nowait()
    assert normalized.symbol == "NIFTY_0"
    assert normalized.timestamp == clock()
    assert (normalized.volume, normalized.open_interest, normalized.average_traded_price) == (1000, 4500, 195)
    stream.shutdown()


def test_history_recovery_fills_startup_morning_and_keeps_future_volume(store):
    agg, client = CandleAggregator(store), Mock()
    client.historical_data.side_effect = lambda *args: [] if args[3] == "day" else minutes(count=60)
    history = HistoryRecovery(store, agg)
    instrument = {"instrument_token": 1, "trading_symbol": "TEST_FUT", "alias": "NIFTY_FUT"}
    history.recover(client, instrument, at(10, 16))
    assert len(store.load("TEST_FUT", "1m")) == 60
    assert store.load("TEST_FUT", "30m")[0]["volume"] == 300
    count = client.historical_data.call_count
    history.recover(client, instrument, at(10, 15, 5))
    assert client.historical_data.call_count == count


def test_history_error_sanitized_and_auth_expiry(engine, capsys):
    from kiteconnect.exceptions import TokenException
    value, _, client = engine
    value.recovery.pause = lambda: None
    client.historical_data.side_effect = RuntimeError("private-secret-test-value")
    value.recover_once()
    assert value.recovery_status == "history_unavailable"
    assert "private-secret-test-value" not in capsys.readouterr().err
    client.historical_data.side_effect = TokenException("private-access-test-value")
    value.recover_once()
    assert value.session.get() is None
    assert value.recovery_status == "authentication_required"


def test_gap_marks_partial_candles_and_clears_volume(store):
    agg = CandleAggregator(store)
    agg.consume(tick(volume=0))
    agg.consume(tick(at(9, 15, 1), volume=10))
    agg.mark_gap()
    agg.advance(at(9, 16, 6))
    candle = store.load("NIFTY 50", "1m")[0]
    assert candle["partial"] and candle["volume"] is None


def test_incomplete_candles_not_used_for_ema():
    rows = [{"completed": True, "partial": False, "close": i, "end_time": str(i)} for i in range(1, 21)]
    rows.append({"completed": False, "partial": False, "close": 10000})
    rows.append({"completed": True, "partial": True, "close": 10000})
    result = momentum(rows)
    assert result["ema20"] == 10.5
    assert result["ema9_above_ema20"] is True
