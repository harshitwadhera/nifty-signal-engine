import pytest


@pytest.fixture(autouse=True)
def isolated_analytics_database(tmp_path, monkeypatch):
    # Legacy API tests also run the real lifespan; never touch the user's database.
    monkeypatch.setenv("MARKET_DB_PATH", str(tmp_path / "test.sqlite3"))
