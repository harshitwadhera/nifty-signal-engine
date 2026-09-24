from datetime import datetime, timedelta, timezone
from threading import RLock

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


class SessionStore:
    """Single-process local session; secrets are never persisted or serialized."""

    def __init__(self, clock=now_ist):
        self.clock = clock
        self.lock = RLock()
        self._token = None
        self._expires_at = None

    def save(self, token):
        with self.lock:
            now = self.clock()
            cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
            self._expires_at = cutoff if now < cutoff else cutoff + timedelta(days=1)
            self._token = token

    def get(self):
        with self.lock:
            if self._expires_at is not None and self.clock() >= self._expires_at:
                self.clear()
            return self._token

    def clear(self):
        with self.lock:
            self._token = self._expires_at = None

    def clear_if(self, token):
        with self.lock:
            if self._token == token:
                self.clear()
