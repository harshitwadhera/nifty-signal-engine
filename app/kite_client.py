"""Shared process-wide budgets for read-only quote and historical requests."""
import time
from threading import Lock


class RequestBudget:
    def __init__(self, spacing, clock=time.monotonic, sleep=time.sleep):
        self.spacing, self.clock, self.sleep = spacing, clock, sleep
        self.lock = Lock()
        self.next_at = 0

    def call(self, action, *args, **kwargs):
        with self.lock:
            delay = self.next_at - self.clock()
            if delay > 0:
                self.sleep(delay)
            self.next_at = self.clock() + self.spacing
        return action(*args, **kwargs)


QUOTE_BUDGET = RequestBudget(1.1)
HISTORY_BUDGET = RequestBudget(0.4)


class ReadOnlyClient:
    def __init__(self, sdk):
        self.sdk = sdk

    def login_url(self):
        return self.sdk.login_url()

    def generate_session(self, *args, **kwargs):
        return self.sdk.generate_session(*args, **kwargs)

    def set_access_token(self, token):
        self.sdk.set_access_token(token)

    def instruments(self, exchange):
        return self.sdk.instruments(exchange)

    def quote(self, symbols):
        return QUOTE_BUDGET.call(self.sdk.quote, symbols)

    def historical_data(self, *args, **kwargs):
        return HISTORY_BUDGET.call(self.sdk.historical_data, *args, **kwargs)
