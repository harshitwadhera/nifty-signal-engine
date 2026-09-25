import json
import sqlite3
from pathlib import Path
from threading import RLock


class CandleStore:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute('''CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL, interval TEXT NOT NULL, candle_start TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume INTEGER, payload TEXT NOT NULL,
            PRIMARY KEY(symbol, interval, candle_start))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS previous_sessions (
            symbol TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(symbol, as_of))''')
        self.db.commit()

    def save(self, candles):
        with self.lock, self.db:
            for c in candles:
                if not c["completed"]:
                    continue
                existing = self.db.execute("SELECT payload FROM candles WHERE symbol=? AND interval=? AND candle_start=?",
                                           (c["symbol"], c["interval"], c["start_time"])).fetchone()
                # Historical data can repair partial/live bars; never downgrade authoritative history.
                if existing:
                    old = json.loads(existing[0])
                    if (old["source"] == "historical" and c["source"] != "historical") or (not old["partial"] and c["partial"]):
                        continue
                self.db.execute("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)",
                                (c["symbol"], c["interval"], c["start_time"], c["open"], c["high"],
                                 c["low"], c["close"], c["volume"], json.dumps(c)))

    def load(self, symbol, interval, limit=100, day=None):
        with self.lock:
            sql = "SELECT payload FROM candles WHERE symbol=? AND interval=?"
            params = [symbol, interval]
            if day:
                sql += " AND substr(candle_start,1,10)=?"
                params.append(str(day))
            sql += " ORDER BY candle_start DESC LIMIT ?"
            params.append(limit)
            return [json.loads(row[0]) for row in reversed(self.db.execute(sql, params).fetchall())]

    def previous(self, symbol, day, value=None):
        with self.lock, self.db:
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO previous_sessions VALUES (?,?,?)", (symbol, str(day), json.dumps(value)))
            row = self.db.execute("SELECT payload FROM previous_sessions WHERE symbol=? AND as_of=?", (symbol, str(day))).fetchone()
            return json.loads(row[0]) if row else None

    def close(self):
        with self.lock:
            self.db.close()
