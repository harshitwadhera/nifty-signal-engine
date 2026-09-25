"""Confine SDK/Twisted operations to its reactor thread, including teardown."""
from threading import Event, Lock, Thread

from kiteconnect import KiteTicker
from twisted.internet import reactor

_reactor_lock = Lock()


def dispatch(action):
    with _reactor_lock:
        if not reactor.running:
            ready = Event()
            reactor.callWhenRunning(ready.set)
            Thread(target=reactor.run, kwargs={"installSignalHandlers": False}, daemon=True).start()
            if not ready.wait(5):
                raise RuntimeError("Feed runtime unavailable")
    reactor.callFromThread(action)


class ManagedTicker(KiteTicker):
    def _create_connection(self, *args, **kwargs):
        super()._create_connection(*args, **kwargs)
        original = self.factory.startedConnecting

        def started(connector):
            # Retain the connector during the initial handshake, not just retries.
            self.factory.connector = connector
            original(connector)
        self.factory.startedConnecting = started


class TickerTransport:
    def __init__(self, api_key, token):
        self.ticker = ManagedTicker(api_key, token, debug=False, reconnect=True,
                                 reconnect_max_tries=5, reconnect_max_delay=30, connect_timeout=10)

    def start(self):
        def connect():
            try:
                self.ticker.connect(threaded=True)
            except Exception:
                self.ticker.on_noreconnect(self.ticker)
        dispatch(connect)

    def close(self):
        # FIFO reactor scheduling guarantees old transport is aborted before a new connect.
        def disconnect():
            self.ticker.stop_retry()
            factory = self.ticker.factory
            connector = getattr(factory, "connector", None)
            if connector is not None:
                connector.disconnect()
            ws = self.ticker.ws
            transport = getattr(ws, "transport", None)
            if transport is not None:
                transport.abortConnection()
        dispatch(disconnect)

    def update_subscriptions(self, added, removed):
        def update():
            try:
                if removed:
                    self.ticker.unsubscribe(removed)
                if added:
                    self.ticker.subscribe(added)
                    self.ticker.set_mode(self.ticker.MODE_FULL, added)
            except Exception:
                # Keep the desired subscription set for automatic resubscription.
                for token in removed:
                    self.ticker.subscribed_tokens.pop(token, None)
                for token in added:
                    self.ticker.subscribed_tokens[token] = self.ticker.MODE_FULL
        dispatch(update)

    @staticmethod
    def flush():
        if reactor.running:
            done = Event()
            reactor.callFromThread(done.set)
            if not done.wait(5):
                raise RuntimeError("Feed shutdown timed out")
