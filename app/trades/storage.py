import json
import sqlite3
from threading import RLock


class TradeJournal:
    def __init__(self, path=':memory:'):
        self.lock = RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS user_trades (
                trade_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE,
                index_name TEXT NOT NULL, status TEXT NOT NULL,
                opened_at TEXT NOT NULL, closed_at TEXT, payload TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_trade_per_index
                ON user_trades(index_name) WHERE closed_at IS NULL;
            CREATE INDEX IF NOT EXISTS user_trades_history ON user_trades(opened_at DESC, trade_id);
            CREATE TABLE IF NOT EXISTS user_trade_events (
                event_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, kind TEXT NOT NULL,
                at TEXT NOT NULL, acknowledged_at TEXT, payload TEXT NOT NULL,
                UNIQUE(trade_id, kind));
        ''')

    def save(self, trade, events=(), create=False):
        payload = json.dumps(trade, allow_nan=False)
        with self.lock, self.db:
            if create:
                try:
                    self.db.execute('INSERT INTO user_trades VALUES (?,?,?,?,?,?,?)',
                        (trade['trade_id'], trade['signal_id'], trade['index_name'], trade['status'],
                         trade['opened_at'], trade['closed_at'], payload))
                except sqlite3.IntegrityError:
                    raise ValueError('Signal already recorded or an open trade exists for this index.') from None
            else:
                self.db.execute('UPDATE user_trades SET status=?,closed_at=?,payload=? WHERE trade_id=?',
                                (trade['status'], trade['closed_at'], payload, trade['trade_id']))
            for event in events:
                self.db.execute('INSERT OR IGNORE INTO user_trade_events VALUES (?,?,?,?,?,?)',
                    (event['event_id'], event['trade_id'], event['kind'], event['at'], None, json.dumps(event, allow_nan=False)))

    def get(self, trade_id):
        with self.lock:
            row = self.db.execute('SELECT payload FROM user_trades WHERE trade_id=?', (trade_id,)).fetchone()
            if not row:
                raise KeyError('Trade not found')
            return json.loads(row[0])

    def listing(self, active=False, limit=25, offset=0, index=None):
        clauses, params = [], []
        if active:
            clauses.append('closed_at IS NULL')
        if index:
            clauses.append('index_name=?')
            params.append(index)
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.lock:
            total = self.db.execute('SELECT count(*) FROM user_trades'+where, params).fetchone()[0]
            items = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM user_trades'+where+
                     ' ORDER BY opened_at DESC,trade_id LIMIT ? OFFSET ?', (*params, limit, offset))]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def events(self, trade_id):
        with self.lock:
            return [{**json.loads(row[0]), 'acknowledged_at': row[1]} for row in self.db.execute(
                'SELECT payload,acknowledged_at FROM user_trade_events WHERE trade_id=? ORDER BY at,event_id', (trade_id,))]

    def acknowledge(self, trade_id, event_id, now):
        with self.lock, self.db:
            cursor = self.db.execute('UPDATE user_trade_events SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE trade_id=? AND event_id=?',
                                    (now, trade_id, event_id))
            if not cursor.rowcount:
                raise KeyError('Event not found')

    def close(self):
        self.db.close()
