from dataclasses import dataclass, field
from pathlib import Path
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    redirect_url: str = "http://127.0.0.1:8000/kite/callback"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        settings = cls(os.getenv("KITE_API_KEY", "").strip(),
                       os.getenv("KITE_API_SECRET", "").strip(),
                       os.getenv("KITE_REDIRECT_URL", cls.redirect_url).strip())
        parsed = urlparse(settings.redirect_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.path != "/kite/callback" or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("KITE_REDIRECT_URL must be an HTTP(S) callback URL")
        return settings
