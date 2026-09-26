from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import sqlite3

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import Settings, DEFAULT_HOSTS, ROOT, parse_allowed_hosts, prepare_database_path
from app.health import database_status
from app.main import create_app
from app.session import SessionStore
from app.analytics.storage import CandleStore


@pytest.mark.parametrize('value', ['', ' ', '*', '*.example.com', 'https://example.com',
    'example.com:443', 'example.com/path', 'a,,b', 'localhost,', 'bad host', '-bad.com', 'bad_.com',
    'example.com\r\nevil.com', 'a'*64+'.com'])
def test_host_parser_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        parse_allowed_hosts(value)


def test_host_parser_trims_normalizes_and_deduplicates(monkeypatch):
    assert parse_allowed_hosts(' trade.example.com, LOCALHOST,localhost,127.0.0.1 ') == (
        'trade.example.com','localhost','127.0.0.1')
    monkeypatch.setenv('APP_ALLOWED_HOSTS','trade.example.com,localhost')
    assert Settings.load().allowed_hosts == ('trade.example.com','localhost')
    assert Settings().allowed_hosts == DEFAULT_HOSTS


def components():
    def worker():
        return SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:True),stop=Event(),start=Mock(),close=Mock())
    stream=Mock()
    stream.status.return_value={'websocket_status':'connected','sensitive':'must-not-be-returned'}
    return dict(stream=stream,engine=Mock(),options=Mock(),signals=worker(),trades=worker())


@pytest.mark.parametrize('host', ['localhost','127.0.0.1','testserver'])
def test_local_hosts_and_liveness_compatible(host):
    with TestClient(create_app(Settings(),**components()),base_url='http://'+host) as client:
        assert client.get('/health').json()=={'status':'ok'}
        assert client.get('/health/live').status_code==200


def test_explicit_production_host_and_untrusted_rejection():
    settings=Settings(allowed_hosts=('trade.example.com','127.0.0.1'))
    with TestClient(create_app(settings,**components()),base_url='https://trade.example.com') as client:
        assert client.get('/health/live').status_code==200
        assert client.get('/health/live',headers={'host':'attacker.example'}).status_code==400
        assert client.get('/health/live',headers={'host':'testserver'}).status_code==400


def test_readiness_no_login_and_database_probe(tmp_path,monkeypatch):
    path=tmp_path/'persistent'/'market.sqlite3'
    monkeypatch.setenv('MARKET_DB_PATH',str(path))
    store=CandleStore(path)
    with TestClient(create_app(Settings(),**components())) as client:
        result=client.get('/health/ready')
        assert result.status_code==503
        assert result.json()=={'status':'degraded','configured':False,'kite_session':'authentication_required',
            'websocket':'connected','signal_engine':'running','trade_monitor':'running','database':'ready'}
        assert client.get('/health/live').status_code==200
    store.close()


def test_mock_healthy_readiness_no_sensitive_data(tmp_path,monkeypatch):
    path=tmp_path/'market.sqlite3';monkeypatch.setenv('MARKET_DB_PATH',str(path));store=CandleStore(path)
    session=SessionStore();session.save('secret-session-marker')
    parts=components()
    with TestClient(create_app(Settings('key-marker','secret-marker'),session=session,**parts)) as client:
        response=client.get('/health/ready');assert response.status_code==200
        assert response.json()['status']=='ready'
        for secret in ('key-marker','secret-marker','secret-session-marker','must-not-be-returned',str(path)):
            assert secret not in response.text
        parts['signals'].stop.set()
        assert client.get('/health/ready').json()['signal_engine']=='stopped'
        assert client.get('/health/ready').status_code==503
    store.close()


def test_database_path_creation_persistence_and_wal(tmp_path,monkeypatch):
    path=tmp_path/'new'/'nested'/'market.sqlite3';monkeypatch.setenv('MARKET_DB_PATH',str(path))
    assert prepare_database_path()==path
    assert path.parent.is_dir() and not path.exists()
    assert database_status(path)=='unavailable' and not path.exists()
    store=CandleStore(path)
    with store.db:
        store.db.execute('CREATE TABLE persistence_test (value TEXT)')
        store.db.execute("INSERT INTO persistence_test VALUES ('retained')")
    assert Path(str(path)+'-wal').exists() and Path(str(path)+'-shm').exists()
    assert database_status(path)=='ready'
    store.close()
    reopened=CandleStore(prepare_database_path())
    assert reopened.db.execute('SELECT value FROM persistence_test').fetchone()[0]=='retained'
    reopened.close()


def test_relative_database_path_is_rooted_at_project(tmp_path,monkeypatch):
    monkeypatch.setattr('app.config.ROOT',tmp_path)
    monkeypatch.setenv('MARKET_DB_PATH','persistent/market.sqlite3')
    assert prepare_database_path()==tmp_path/'persistent'/'market.sqlite3'


@pytest.mark.parametrize('value',['',':memory:','file:temporary?mode=memory'])
def test_database_path_never_falls_back(value,monkeypatch):
    monkeypatch.setenv('MARKET_DB_PATH',value)
    with pytest.raises(ValueError):
        with TestClient(create_app(Settings(),**components())):
            pass


def test_invalid_database_parent_fails_startup(tmp_path,monkeypatch):
    blocked=tmp_path/'blocked';blocked.write_text('file')
    monkeypatch.setenv('MARKET_DB_PATH',str(blocked/'market.sqlite3'))
    with pytest.raises(OSError):
        with TestClient(create_app(Settings(),**components())):
            pass


def test_shutdown_order_and_cleanup_after_start_failure():
    for fail in (False,True):
        parts=components();closed=[]
        for name,method in [('engine','shutdown'),('stream','shutdown'),('options','shutdown'),('signals','close'),('trades','close')]:
            getattr(parts[name],method).side_effect=lambda name=name: closed.append(name)
        if fail: parts['options'].start.side_effect=RuntimeError('fixture start failure')
        try:
            with TestClient(create_app(Settings(),**parts)):
                assert not fail
        except RuntimeError:
            assert fail
        assert closed==['trades','signals','options','stream','engine']


def test_real_shutdown_stops_workers_and_closes_journals(monkeypatch):
    import app.main as main
    saved={}
    for name in ('MarketEngine','OptionsService','KiteStream'):
        original=getattr(main,name)
        def capture(*args,_original=original,_name=name,**kwargs):
            saved[_name]=_original(*args,**kwargs)
            return saved[_name]
        monkeypatch.setattr(main,name,capture)
    application=create_app(Settings())
    with TestClient(application) as client:
        assert client.get('/health/live').status_code==200
        signals,trades,breadth=application.state.signals,application.state.trades,application.state.breadth
    for worker in (signals,trades,breadth):
        assert not worker.thread.is_alive()
    for service in ('MarketEngine','OptionsService'):
        assert all(not thread.is_alive() for thread in saved[service].threads)
    assert not saved['KiteStream']._thread.is_alive()
    databases=[signals.lifecycle.journal.db,trades.journal.db,breadth.db.db,
               saved['MarketEngine'].store.db,saved['OptionsService'].database.db]
    for database in databases:
        with pytest.raises(sqlite3.ProgrammingError):database.execute('SELECT 1')


def test_https_proxy_cookie_and_same_origin_protection():
    parts=components();parts['trades'].acknowledge=Mock(return_value={'acknowledged':True})
    kite=Mock();kite.login_url.return_value='https://kite.zerodha.com/connect/login?api_key=fixture'
    settings=Settings('fixture','fixture-secret','https://trade.example.com/kite/callback',('trade.example.com',))
    application=create_app(settings,client_factory=lambda:kite,**parts)
    # TestClient uses peer 'testclient'; only this explicit peer is trusted here.
    with TestClient(ProxyHeadersMiddleware(application,trusted_hosts=['testclient']),base_url='http://trade.example.com') as client:
        headers={'X-Forwarded-Proto':'https','Origin':'https://trade.example.com'}
        login=client.get('/kite/login',headers=headers,follow_redirects=False)
        assert login.status_code==303 and 'Secure' in login.headers['set-cookie']
        assert client.post('/api/trades/t/acknowledge',headers=headers,json={'event_id':'e'}).status_code==200
        assert client.post('/api/trades/t/acknowledge',headers={**headers,'Origin':'https://evil.example'},json={'event_id':'e'}).status_code==403
        assert client.post('/api/trades/t/acknowledge',headers={**headers,'Sec-Fetch-Site':'cross-site'},json={'event_id':'e'}).status_code==403
    with TestClient(ProxyHeadersMiddleware(create_app(settings,**components()),trusted_hosts=['127.0.0.1']),base_url='http://trade.example.com') as client:
        assert client.post('/api/trades/t/acknowledge',headers=headers,json={'event_id':'e'}).status_code==403
