from datetime import date, datetime, timedelta
from unittest.mock import Mock
import json

import pytest
from fastapi.testclient import TestClient

from app.analytics.session import IST
from app.config import Settings
from app.kite_client import RequestBudget
from app.main import create_app
from app.options.analytics import OptionsAnalytics, enrich, liquidity, max_pain, pcr, positioning, walls
from app.options.config import OptionsConfig
from app.options.discovery import OptionDiscovery, SubscriptionWindow, atm
from app.options.pricing import implied_greeks, price_greeks
from app.options.service import OptionsService
from app.options.state import OptionBook, normalize_quote
from app.session import SessionStore
from app.streaming.instruments import InstrumentResolver
from app.streaming.kite_stream import KiteStream
from app.streaming.market_state import MarketState, Tick


def now():
    return datetime(2026, 9, 25, 10, tzinfo=IST)


def metadata(strikes=range(80, 121, 2)):
    rows = []
    token = 100
    for name in ("NIFTY", "BANKNIFTY"):
        for expiry in (date(2026, 9, 28), date(2026, 9, 30)):
            for strike in strikes:
                for kind in ("CE", "PE"):
                    token += 1
                    rows.append(dict(exchange="NFO", name=name, expiry=expiry, strike=strike,
                                     instrument_type=kind, segment="NFO-OPT", lot_size=25,
                                     instrument_token=token, tradingsymbol=f"{name}{expiry:%m%d}{strike}{kind}"))
        rows.append(dict(exchange="NFO", name=name, expiry=date(2026, 9, 30), instrument_type="FUT", segment="NFO-FUT", instrument_token=token+1000, tradingsymbol=name+"FUT"))
    return rows


def raw(clock, kind="CE", price=10, oi=None):
    return {"last_price": price, "oi": oi if oi is not None else (1000 if kind == "CE" else 2000),
            "volume": 100 if kind == "CE" else 300, "timestamp": clock(),
            "depth": {"buy": [{"price": price-0.01, "quantity": 50, "orders": 1}],
                      "sell": [{"price": price+0.01, "quantity": 60, "orders": 2}]}}


@pytest.fixture
def service(tmp_path):
    clock = Mock(return_value=now())
    client = Mock()
    client.instruments.return_value = metadata()
    client.quote.side_effect = lambda symbols: {s: raw(clock, "PE" if s.endswith("PE") else "CE") for s in symbols}
    state = MarketState(clock)
    state.reset([{"instrument_token": 1, "trading_symbol": "NIFTY 50", "friendly_name": "NIFTY"},
                 {"instrument_token": 2, "trading_symbol": "NIFTY BANK", "friendly_name": "BANK"}])
    for token, symbol in ((1, "NIFTY 50"), (2, "NIFTY BANK")):
        state.update(Tick(token, symbol, 100, clock(), clock()))
    stream = Mock()
    stream.state = state
    stream.resolver = InstrumentResolver(clock)
    session = SessionStore(clock)
    session.save("dummy-session")
    options = OptionsService(stream, session, lambda: client, tmp_path/"options.sqlite3", clock=clock)
    options.discovery.refresh(client)
    yield options, client, clock
    options.shutdown()


def test_expiry_strike_ce_pe_and_monthly_discovery(service):
    options, _, _ = service
    catalog = options.discovery
    assert catalog.get_expiries("NIFTY") == [date(2026, 9, 28), date(2026, 9, 30)]
    assert catalog.selections("BANKNIFTY") == {"nearest": date(2026, 9, 28), "next": date(2026, 9, 30), "monthly": date(2026, 9, 30)}
    ce = catalog.get_option_contract("NIFTY", date(2026, 9, 28), 100, "CE")
    pe = catalog.get_option_contract("NIFTY", date(2026, 9, 28), 100, "PE")
    assert ce.instrument_token != pe.instrument_token and ce.lot_size == 25
    assert atm([95, 100, 110], 106) == 110
    assert atm([95, 100], 97.5) == 95


def test_expiry_rollover_without_weekday_assumptions(service):
    options, client, clock = service
    clock.return_value = datetime(2026, 9, 28, 15, 30, tzinfo=IST)
    assert options.discovery.selections("NIFTY")["nearest"] == date(2026, 9, 30)
    client.instruments.return_value = [r for r in metadata() if r["expiry"] != date(2026, 9, 30)]
    options.discovery.resolver.invalidate("NFO")
    options.discovery.refresh(client)
    assert options.discovery.get_expiries("NIFTY") == []


def test_subscription_window_hysteresis(service):
    options, _, _ = service
    expiry = date(2026, 9, 28)
    contracts = options.discovery.chain("NIFTY", expiry)
    window = SubscriptionWindow(radius=3)
    first = window.select("NIFTY", expiry, contracts, 100)
    assert len(first) == 14
    assert window.select("NIFTY", expiry, contracts, 102) == first
    moved = window.select("NIFTY", expiry, contracts, 106)
    assert moved != first and {c.option_type for c in moved} == {"CE", "PE"}


def test_session_oi_changes_and_previous_definition(service):
    options, _, clock = service
    c = options.discovery.get_option_contract("NIFTY", date(2026, 9, 28), 100, "CE")
    book = OptionBook()
    book.update(c, normalize_quote(raw(clock, oi=1000), clock(), "rest"), clock())
    clock.return_value += timedelta(seconds=2)
    second = raw(clock, price=11, oi=1100)
    second["volume"] = 130
    book.update(c, normalize_quote(second, clock(), "rest"), clock())
    row = book.row(c, previous_oi=900)
    assert row["oi_change_session"] == 100
    assert row["oi_change_vs_previous_close"] == 200
    assert row["oi_change_percent"] == 10 and row["price_change"] == 1
    assert row["volume_change"] == 30
    assert book.row(c)["oi_change_vs_previous_close"] is None
    book.clear()
    assert book.row(c)["oi_change_session"] is None


@pytest.mark.parametrize("price,oi,expected", [(2,3,"LONG_BUILDUP"),(-2,3,"SHORT_BUILDUP"),(2,-3,"SHORT_COVERING"),(-2,-3,"LONG_UNWINDING"),(0.1,3,"NEUTRAL"),(2,0.1,"NEUTRAL"),(None,3,"UNAVAILABLE")])
def test_positioning_thresholds(price, oi, expected):
    assert positioning({"price_change_percent": price, "oi_change_percent": oi}, OptionsConfig()) == expected


def test_full_chain_pcr_near_atm_and_coverage(service):
    options, _, _ = service
    options.cycle(force=True)
    summary, rows = options.response("NIFTY", window=3)
    assert summary["pcr_oi"] == 2 and summary["pcr_volume"] == 3
    assert summary["near_atm_pcr_oi"] == 2
    assert summary["near_atm_window"] == 3
    assert summary["coverage"]["percent"] == 100 and not summary["stale"]
    assert summary["atm"] == 100 and summary["atm_ce"]["option_type"] == "CE"
    assert rows[0]["distance_from_atm"] == -20
    json.dumps(summary, allow_nan=False)


def test_partial_chain_suppresses_full_pcr_walls_max_pain(service):
    options, client, clock = service
    client.quote.side_effect = lambda symbols: {s: raw(clock) for s in symbols[1:]}
    options.cycle(force=True)
    summary, _ = options.response("NIFTY")
    assert summary["pcr_oi"] is None and summary["max_pain"] is None
    assert summary["call_oi_wall"] is None and summary["stale"]
    assert summary["coverage"]["received_contracts"] < summary["coverage"]["expected_contracts"]


def test_zero_call_denominator_and_missing_fields():
    rows = [{"option_type": "CE", "oi": 0}, {"option_type": "PE", "oi": 100}]
    assert pcr(rows, "oi") is None
    rows[0]["oi"] = None
    assert pcr(rows, "oi") is None


def test_walls_additions_unwinding_and_max_pain():
    rows = [{"strike": 90, "option_type": "CE", "oi": 10, "oi_change_session": 2, "positioning": "SHORT_BUILDUP"},
            {"strike": 100, "option_type": "CE", "oi": 50, "oi_change_session": 10, "positioning": "LONG_BUILDUP"},
            {"strike": 100, "option_type": "PE", "oi": 60, "oi_change_session": -20, "positioning": "LONG_UNWINDING"},
            {"strike": 110, "option_type": "PE", "oi": 20, "oi_change_session": 5, "positioning": "SHORT_BUILDUP"}]
    result = walls(rows)
    assert result["call_oi_wall"] == result["put_oi_wall"] == 100
    assert result["highest_call_oi_addition"] == {"strike": 100, "value": 10}
    assert result["largest_put_unwinding"] == {"strike": 100, "value": -20}
    assert result["call_writing_zones"][0]["strike"] == 90
    assert max_pain(rows) == 100
    brute = min([90,100,110], key=lambda s: sum(r["oi"]*max(0, s-r["strike"] if r["option_type"]=="CE" else r["strike"]-s) for r in rows))
    assert max_pain(rows) == brute


def test_black_scholes_reference_values():
    result = price_greeks(100,100,1,0.05,0.2,"CE","black_scholes_q0")
    assert result["price"] == pytest.approx(10.4505835722)
    assert result["delta"] == pytest.approx(0.6368306512)
    assert result["gamma"] == pytest.approx(0.01876201735)
    assert result["vega"] == pytest.approx(0.3752403469)
    assert result["theta"] == pytest.approx(-6.414027546/365)
    assert implied_greeks(result["price"],100,100,1,0.05,"CE","black_scholes_q0")["iv"] == pytest.approx(0.2)


@pytest.mark.parametrize("model", ["black76", "black_scholes_q0"])
@pytest.mark.parametrize("kind", ["CE", "PE"])
def test_greeks_against_finite_differences(model, kind):
    args = (100,105,0.5,0.06,0.3,kind,model)
    result = price_greeks(*args)
    def p(f=100,t=.5,s=.3):
        return price_greeks(f,105,t,.06,s,kind,model)["price"]
    step=.001
    assert result["delta"] == pytest.approx((p(f=100+step)-p(f=100-step))/(2*step), rel=1e-5)
    assert result["gamma"] == pytest.approx((p(f=100+step)-2*p()+p(f=100-step))/step**2, rel=1e-4)
    assert result["vega"] == pytest.approx((p(s=.30001)-p(s=.29999))/.00002/100, rel=1e-5)
    assert result["theta"] == pytest.approx(-(p(t=.50001)-p(t=.49999))/.00002/365, rel=1e-5)
    assert implied_greeks(p(),100,105,.5,.06,kind,model)["iv"] == pytest.approx(.3)


@pytest.mark.parametrize("price,future,t", [(0,100,1),(200,100,1),(10,0,1),(10,100,0),(float('nan'),100,1),(10,float('inf'),1),(-1,100,1),(None,100,1)])
def test_invalid_iv(price, future, t):
    assert all(v is None for v in implied_greeks(price,future,100,t,.06,"CE","black76").values())


def test_spread_and_liquidity():
    row = {"bid": 99.5, "ask": 100, "bid_quantity": 10, "ask_quantity": 10, "oi": 2000, "volume": 1000}
    assert liquidity(row,OptionsConfig())["liquidity_state"] == "LIQUID"
    row["bid"] = 98
    assert liquidity(row,OptionsConfig())["liquidity_state"] == "MODERATE"
    row["bid"] = 101
    assert liquidity(row,OptionsConfig()) == {"spread": None,"spread_percent": None,"liquidity_state":"POOR"}
    row["bid"] = None
    assert liquidity(row,OptionsConfig())["spread"] is None


def test_staleness_and_expired_session(service):
    options, _, clock = service
    options.cycle(force=True)
    clock.return_value += timedelta(seconds=61)
    summary, rows = options.response("NIFTY")
    assert summary["stale"] and summary["pcr_oi"] is None
    assert all(row["iv"] is None for row in rows)
    options.session.clear()
    options.cycle()
    assert options.status == "authentication_required"
    options.stream.set_option_contracts.assert_called_with([])


def test_previous_close_oi_cached_exact_session(service):
    options, client, clock = service
    options.cycle(force=True)
    options.database.previous("NIFTY 50", clock().date(), {"session_date":"2026-09-24","high":1,"low":1,"close":1})
    client.historical_data.return_value = [{"date": clock()-timedelta(days=1), "oi": 850}]
    options.baseline_once()
    contract = options.discovery.get_option_contract("NIFTY", date(2026,9,28),100,"CE")
    assert options.store.previous(contract.trading_symbol, clock().date())["oi"] == 850
    assert client.historical_data.call_args.kwargs == {"oi": True}
    options.previous.clear()
    options._baseline_cursor=0
    options.baseline_once()
    assert client.historical_data.call_count == 1


def test_old_oi_is_not_previous_session_close(service):
    options, client, clock = service
    options.cycle(force=True)
    options.database.previous("NIFTY 50", clock().date(), {"session_date":"2026-09-24"})
    client.historical_data.return_value = [{"date": clock()-timedelta(days=2), "oi": 800}]
    options.baseline_once()
    assert next(iter(options.previous.values()))["oi"] is None


def test_minute_snapshot_persistence_no_duplicates(service):
    options, _, _ = service
    options.cycle(force=True)
    options.cycle(force=True)
    rows = options.database.db.execute("SELECT payload FROM option_snapshots").fetchall()
    assert len(rows) == 2
    payload = json.loads(rows[0][0])
    assert len(payload["contracts"]) <= 22
    assert payload["pcr_oi"] == 2
    assert "dummy-session" not in rows[0][0]


def test_quote_batch_bound_and_partial_failure(service):
    options, client, clock = service
    client.instruments.return_value = metadata(range(1, 150))
    options.discovery.resolver.invalidate("NFO")
    options.discovery.refresh(client)
    count=0
    def quote(symbols):
        nonlocal count
        count+=1
        assert len(symbols)<=200
        if count == 2:
            raise RuntimeError("private error")
        return {s:raw(clock) for s in symbols}
    client.quote.side_effect=quote
    options.refresh_chain(client,"NIFTY",date(2026,9,28),options.session.get())
    summary,_ = options.response("NIFTY")
    assert summary["stale"] and summary["pcr_oi"] is None


def test_request_budget():
    time=[0.0]
    calls=[]
    budget=RequestBudget(1.1,lambda:time[0],lambda delay:time.__setitem__(0,time[0]+delay))
    for _ in range(3):
        budget.call(lambda:calls.append(time[0]))
    assert calls == pytest.approx([0,1.1,2.2])


def test_option_endpoints_validation_and_pagination(service):
    options, _, _ = service
    options.cycle(force=True)
    wrapper=Mock(wraps=options)
    wrapper.start=Mock(); wrapper.shutdown=Mock()
    with TestClient(create_app(Settings(),options=wrapper)) as browser:
        assert browser.get('/api/options/nifty').status_code==200
        result=browser.get('/api/options/nifty/chain?strike_min=98&strike_max=102&limit=2').json()
        assert result['total_matching']==6 and len(result['contracts'])==2
        for url in ('/api/options/other','/api/options/nifty?window=0','/api/options/nifty?expiry=bad','/api/options/nifty/chain?limit=501','/api/options/nifty/chain?strike_min=NaN','/api/options/nifty/chain?strike_min=102&strike_max=98'):
            assert browser.get(url).status_code==422
        assert browser.get('/api/options/nifty?expiry=2026-01-01').status_code==404


def test_reconnect_reapplies_options_without_another_connection(service):
    options, client, clock = service
    state=options.stream.state
    transport=Mock()
    resolver=Mock()
    resolver.clock=clock
    resolver.indices.return_value=[{"instrument_token":1,"trading_symbol":"NIFTY 50","friendly_name":"NIFTY"}]
    feed=KiteStream(Settings('dummy','dummy'),options.session,lambda:client,resolver=resolver,state=state,transport_factory=lambda *args:transport)
    contracts=options.discovery.chain('NIFTY',date(2026,9,28))[:2]
    feed.set_option_contracts(contracts)
    feed.reconcile()
    ticker=transport.ticker
    ticker.on_connect(ticker,{})
    ticker.on_close(ticker,1006,'temporary')
    ticker.on_reconnect(ticker,1)
    ticker.subscribed_tokens={1:'full',99999:'full'}
    ticker.on_connect(ticker,{})
    ticker.unsubscribe.assert_called_with([99999])
    assert ticker.subscribe.call_args.args[0]==[1]+[c.instrument_token for c in contracts]
    transport.start.assert_called_once()
    feed.shutdown()


def test_explicit_expiry_not_overwritten_by_default_summary(service):
    options, _, _ = service
    options.response('NIFTY',date(2026,9,30))
    options.response('NIFTY')
    assert options.requested['NIFTY']==date(2026,9,30)


def test_black76_reference_and_json_guard():
    from app.options.state import finite_json
    assert price_greeks(100,100,1,.05,.2,'CE','black76')['price']==pytest.approx(7.5770821464)
    assert finite_json({'bad':float('inf'),'list':[float('nan')]})=={'bad':None,'list':[None]}


def test_baseline_does_not_use_preopen_or_future_observation(service):
    options, _, clock=service
    contract=options.discovery.get_option_contract('NIFTY',date(2026,9,28),100,'CE')
    book=OptionBook()
    clock.return_value=clock().replace(hour=9,minute=0)
    quote=normalize_quote(raw(clock),clock(),'rest')
    book.update(contract,quote,clock())
    assert book.row(contract)['oi_session_baseline'] is None
    clock.return_value=clock().replace(hour=10)
    book.update(contract,normalize_quote(raw(clock),clock(),'rest'),clock())
    assert book.row(contract,quote)['oi_change_session'] is None


def test_auth_error_does_not_leak_or_crash_feed(service,capsys):
    from kiteconnect.exceptions import TokenException
    options,client,_=service
    client.quote.side_effect=TokenException('private-access-token-value')
    options.cycle(force=True)
    assert options.session.get() is None
    assert options.status=='authentication_required'
    assert 'private-access-token-value' not in capsys.readouterr().err


def test_config_validation():
    with pytest.raises(ValueError):
        OptionsConfig(refresh_seconds=1)
    with pytest.raises(ValueError):
        OptionsConfig(risk_free_rate=float('nan'))


def test_live_window_overlay_does_not_contaminate_full_pcr(service):
    options, _, clock=service
    options.cycle(force=True)
    contract=options.discovery.get_option_contract('NIFTY',date(2026,9,28),100,'CE')
    clock.return_value+=timedelta(seconds=2)
    quote=raw(clock,oi=5000)
    quote['volume_traded']=500
    quote=normalize_quote(quote,clock(),'stream')
    options.process_tick(options._generation,contract.instrument_token,quote,clock())
    summary,rows=options.response('NIFTY')
    assert summary['pcr_oi']==2
    assert summary['atm_ce']['oi']==5000 and summary['atm_ce']['source']=='stream'
    selected=next(r for r in rows if r['trading_symbol']==contract.trading_symbol)
    assert selected['depth']['buy'][0]['quantity']==50
    assert selected['volume']==500


def test_failed_refresh_does_not_reuse_old_full_quotes(service):
    options,client,clock=service
    options.cycle(force=True)
    assert options.response('NIFTY')[0]['pcr_oi']==2
    client.quote.side_effect=lambda symbols:{s:raw(clock) for s in symbols[1:]}
    options.cycle(force=True)
    assert options.response('NIFTY')[0]['pcr_oi'] is None


def test_persisted_timestamp_follows_network_fetch(service):
    options,client,clock=service
    def quote(symbols):
        clock.return_value+=timedelta(seconds=5)
        return {s:raw(clock) for s in symbols}
    client.quote.side_effect=quote
    options.cycle(force=True)
    rows=options.database.db.execute('SELECT timestamp FROM option_snapshots').fetchall()
    assert rows and all(datetime.fromisoformat(row[0])>=clock() for row in rows)
