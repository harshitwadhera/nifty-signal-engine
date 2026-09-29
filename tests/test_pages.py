from html.parser import HTMLParser
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.mark.parametrize('route,title,scripts,present,absent', [
    ('/', 'Trading Dashboard', ['market_hours.js', 'dashboard.js', 'charts.js', 'signals.js', 'trades.js'],
     ['nifty-chart-frame', 'nifty-signal', 'nifty-trade', 'status'], ['nifty-structure', 'nifty-options']),
    ('/analysis', 'Market Analysis', ['market_hours.js', 'structure.js', 'options.js'],
     ['nifty-structure', 'nifty-options', 'nifty-options-expiry'], ['nifty-chart-frame', 'nifty-signal', 'nifty-trade', 'status']),
])
def test_static_page_routes_and_assets(route, title, scripts, present, absent):
    broker = Mock()
    with TestClient(create_app(Settings(), client_factory=broker, stream=Mock(), engine=Mock(), options=Mock())) as client:
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/html')
        assert response.headers['cache-control'] == 'no-store'
        assert "script-src 'self'" in response.headers['content-security-policy']
        assert f'<h1>{title}</h1>' in response.text
        page = Page(response.text)
        ids = [attrs['id'] for _, attrs in page.tags if 'id' in attrs]
        assert len(ids) == len(set(ids))
        assert all(value in ids for value in present)
        assert all(value not in ids for value in absent)
        actual_scripts = [attrs for tag, attrs in page.tags if tag == 'script']
        assert [attrs['src'] for attrs in actual_scripts] == ['/static/'+name for name in scripts]
        assert all('defer' in attrs and 'async' not in attrs for attrs in actual_scripts)
        links = [attrs for tag, attrs in page.tags if tag == 'a']
        assert {'/', '/analysis'} <= {attrs['href'] for attrs in links}
        assert [attrs['href'] for attrs in links if attrs.get('aria-current') == 'page'] == [route]
        for asset in ['/static/style.css', *(attrs['src'] for attrs in actual_scripts)]:
            assert client.get(asset).status_code == 200
        assert client.get('/health').json() == {'status': 'ok'}
        assert client.post(route).status_code == 405
    broker.assert_not_called()
