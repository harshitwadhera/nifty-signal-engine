from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from kiteconnect.exceptions import TokenException

from app.config import Settings
from app.main import create_app
from app.market import SYMBOLS
from app.session import IST, SessionStore


@pytest.fixture
def setup():
    kite = Mock(spec=["login_url", "generate_session", "set_access_token", "quote"])
    kite.login_url.return_value = "https://kite.zerodha.com/connect/login?api_key=test-key&v=3"
    kite.generate_session.return_value = {"access_token": "private-access-value"}
    kite.quote.return_value = {s: {"last_price": 100.25} for s in SYMBOLS}
    session = SessionStore()
    app = create_app(Settings("test-key", "private-secret-value"), lambda: kite, session)
    with TestClient(app) as client:
        yield client, kite, session


def begin(client):
    response = client.get("/kite/login", follow_redirects=False)
    assert response.status_code == 303
    params = parse_qs(urlparse(response.headers["location"]).query)
    return parse_qs(params["redirect_params"][0])["state"][0]


def test_health_dashboard_and_static(setup):
    client, _, _ = setup
    assert client.get("/health").json() == {"status": "ok"}
    assert "BANK NIFTY" in client.get("/").text
    assert client.get("/static/dashboard.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_disconnected(setup):
    client, kite, _ = setup
    assert client.get("/api/market/snapshot").status_code == 401
    assert client.get("/api/connection").json()["connection_status"] == "disconnected"
    kite.quote.assert_not_called()


def test_authentication_and_replay(setup, capsys):
    client, kite, session = setup
    state = begin(client)
    response = client.get("/kite/callback", params={"state": state, "request_token": "private-request-value", "status": "success"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert session.get() == "private-access-value"
    kite.generate_session.assert_called_once_with("private-request-value", api_secret="private-secret-value")
    assert client.get("/kite/callback", params={"state": state, "request_token": "replayed"}).status_code == 400
    output = response.text + str(response.headers) + capsys.readouterr().err
    for value in ("private-access-value", "private-secret-value", "private-request-value"):
        assert value not in output


@pytest.mark.parametrize("query", [{}, {"state": "invalid", "request_token": "x"}])
def test_invalid_state(setup, query):
    client, kite, _ = setup
    assert client.get("/kite/callback", params=query).status_code == 400
    kite.generate_session.assert_not_called()


def test_missing_request_token(setup):
    client, kite, _ = setup
    state = begin(client)
    assert client.get("/kite/callback", params={"state": state}).status_code == 400
    kite.generate_session.assert_not_called()


def test_exchange_error_is_sanitized(setup, capsys):
    client, kite, session = setup
    kite.generate_session.side_effect = RuntimeError("private-secret-value private-access-value")
    response = client.get("/kite/callback", params={"state": begin(client), "request_token": "x"})
    assert response.status_code == 502
    assert session.get() is None
    output = response.text + capsys.readouterr().err
    assert "private-secret-value" not in output
    assert "private-access-value" not in output


def test_snapshot(setup):
    client, kite, session = setup
    session.save("private-access-value")
    response = client.get("/api/market/snapshot")
    data = response.json()
    assert response.status_code == 200
    assert [q["symbol"] for q in data["instruments"]] == list(SYMBOLS)
    assert all(q["value"] == 100.25 for q in data["instruments"])
    assert data["last_updated"] and data["partial"] is False
    assert "private-access-value" not in response.text
    kite.quote.assert_called_once_with(list(SYMBOLS))


def test_missing_quotes(setup):
    client, kite, session = setup
    session.save("token")
    kite.quote.return_value = {}
    data = client.get("/api/market/snapshot").json()
    assert data["partial"] is True
    assert all(q["value"] is None for q in data["instruments"])


@pytest.mark.parametrize("error,status,cleared", [(TokenException("sensitive"), 401, True), (RuntimeError("sensitive"), 502, False)])
def test_market_errors(setup, error, status, cleared):
    client, kite, session = setup
    session.save("token")
    kite.quote.side_effect = error
    response = client.get("/api/market/snapshot")
    assert response.status_code == status
    assert "sensitive" not in response.text
    assert (session.get() is None) is cleared


def test_unconfigured():
    with TestClient(create_app(Settings())) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/kite/login").status_code == 503
        assert client.get("/api/market/snapshot").status_code == 503


@pytest.mark.parametrize("hour", [5, 12])
def test_expiry(hour):
    now = datetime(2026, 9, 24, hour, tzinfo=IST)
    store = SessionStore(lambda: now)
    store.save("token")
    assert store.get() == "token"
    now = now.replace(hour=6) + (timedelta(days=1) if hour >= 6 else timedelta())
    assert store.get() is None


def test_security_headers_and_methods(setup):
    client, _, _ = setup
    response = client.get("/health")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert client.post("/api/market/snapshot").status_code == 405
    assert client.get("/orders").status_code == 404
    assert client.get("/health", headers={"host": "untrusted.example"}).status_code == 400


def test_state_requires_same_browser(setup):
    client, kite, _ = setup
    state = begin(client)
    client.cookies.clear()
    assert client.get("/kite/callback", params={"state": state, "request_token": "x"}).status_code == 400
    kite.generate_session.assert_not_called()


def test_login_state_timeout(setup, monkeypatch):
    client, kite, _ = setup
    state = begin(client)
    monkeypatch.setattr("app.main.time.monotonic", lambda: float("inf"))
    assert client.get("/kite/callback", params={"state": state, "request_token": "x"}).status_code == 400
    kite.generate_session.assert_not_called()


def test_unexpected_exception_is_sanitized(setup, capsys):
    client, kite, _ = setup
    kite.login_url.side_effect = RuntimeError("private-secret-value")
    response = client.get("/kite/login")
    assert response.status_code == 500
    assert "private-secret-value" not in response.text + capsys.readouterr().err
