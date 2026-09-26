from dataclasses import dataclass, field
from pathlib import Path
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOSTS = ('127.0.0.1', 'localhost', 'testserver')


def parse_allowed_hosts(value):
    """Exact DNS names/IPv4 only: no URLs, ports, wildcards or empty entries."""
    hosts = tuple(dict.fromkeys(part.strip().lower() for part in value.split(',')))
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if not hosts or any(len(host) > 253 or not re.fullmatch(label+r'(?:\.'+label+r')*', host) for host in hosts):
        raise ValueError('APP_ALLOWED_HOSTS must contain comma-separated exact hostnames without ports or wildcards')
    return hosts


def prepare_database_path():
    value = os.getenv('MARKET_DB_PATH', 'data/market.sqlite3').strip()
    if not value or value == ':memory:' or value.startswith('file:'):
        raise ValueError('MARKET_DB_PATH must name a persistent SQLite file')
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_dir():
        raise ValueError('MARKET_DB_PATH must name a file, not a directory')
    return path


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    redirect_url: str = "http://127.0.0.1:8000/kite/callback"
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS

    def __post_init__(self):
        object.__setattr__(self, 'allowed_hosts', parse_allowed_hosts(','.join(self.allowed_hosts)))

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        settings = cls(os.getenv("KITE_API_KEY", "").strip(),
                       os.getenv("KITE_API_SECRET", "").strip(),
                       os.getenv("KITE_REDIRECT_URL", cls.redirect_url).strip(),
                       parse_allowed_hosts(os.getenv('APP_ALLOWED_HOSTS', ','.join(DEFAULT_HOSTS))))
        parsed = urlparse(settings.redirect_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.path != "/kite/callback" or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("KITE_REDIRECT_URL must be an HTTP(S) callback URL")
        return settings
