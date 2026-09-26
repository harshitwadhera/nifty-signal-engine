"""Bounded local component probes. Never return exception text or credentials."""
import sqlite3


def database_status(path):
    connection = None
    try:
        if path is None or not path.is_file():
            return 'unavailable'
        # mode=rw prevents a readiness request from creating a replacement DB.
        connection = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=.25)
        if connection.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
            return 'unavailable'
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('SELECT 1').fetchone()
        connection.rollback()
        return 'ready'
    except (OSError, sqlite3.Error):
        return 'unavailable'
    finally:
        if connection is not None:
            connection.close()


def worker_status(service):
    if service is None:
        return 'unavailable'
    try:
        return 'running' if service.thread and service.thread.is_alive() and not service.stop.is_set() else 'stopped'
    except (AttributeError, RuntimeError):
        return 'unavailable'


def readiness(settings, session, stream, signals, trades, path):
    websocket = 'unavailable'
    try:
        value = stream.status().get('websocket_status')
        if value in {'connected', 'connecting', 'reconnecting', 'disconnected', 'authentication_required'}:
            websocket = value
    except Exception:
        pass
    result = {'configured': settings.configured, 'kite_session': 'available' if session.get() else 'authentication_required',
              'websocket': websocket, 'signal_engine': worker_status(signals),
              'trade_monitor': worker_status(trades), 'database': database_status(path)}
    ready = (result['configured'] and result['kite_session'] == 'available' and websocket == 'connected'
             and result['signal_engine'] == result['trade_monitor'] == 'running' and result['database'] == 'ready')
    return {'status': 'ready' if ready else 'degraded', **result}
