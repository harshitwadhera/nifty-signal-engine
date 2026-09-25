import json

from app.analytics.session import local
from app.options.state import finite_json


CHAIN_FIELDS = (
    "strike", "option_type", "trading_symbol", "instrument_token", "ltp", "bid", "ask",
    "bid_quantity", "ask_quantity", "spread", "spread_percent", "oi", "oi_change_session",
    "oi_change_vs_previous_close", "volume", "volume_change", "iv", "delta", "gamma",
    "theta", "vega", "liquidity", "positioning", "source", "quote_timestamp", "stale",
)


class OptionsStore:
    """Share the existing SQLite connection/lock. Never store credentials or raw ticks."""
    def __init__(self, candle_store):
        self.store = candle_store
        with self.store.lock, self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS option_previous_oi (
                symbol TEXT NOT NULL, as_of TEXT NOT NULL, oi REAL, session_date TEXT,
                PRIMARY KEY(symbol, as_of))''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS option_snapshots (
                index_name TEXT NOT NULL, expiry TEXT NOT NULL, minute TEXT NOT NULL,
                timestamp TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(index_name, expiry, minute))''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS option_chain_snapshots (
                index_name TEXT NOT NULL, expiry TEXT NOT NULL,
                snapshot_minute TEXT NOT NULL, timestamp TEXT NOT NULL,
                strike REAL NOT NULL, option_type TEXT NOT NULL,
                trading_symbol TEXT, instrument_token INTEGER,
                ltp REAL, bid REAL, ask REAL, bid_quantity INTEGER, ask_quantity INTEGER,
                spread REAL, spread_percent REAL, oi REAL, oi_change_session REAL,
                oi_change_vs_previous_close REAL, volume REAL, volume_change REAL,
                iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
                liquidity TEXT, positioning TEXT, source TEXT, quote_timestamp TEXT,
                stale INTEGER,
                PRIMARY KEY(index_name, expiry, snapshot_minute, strike, option_type))''')
            self.store.db.execute('''CREATE INDEX IF NOT EXISTS option_chain_contract_history
                ON option_chain_snapshots(index_name, expiry, strike, option_type, snapshot_minute)''')
            self.store.db.execute('''CREATE INDEX IF NOT EXISTS option_chain_time
                ON option_chain_snapshots(snapshot_minute, index_name)''')

    def save_chain(self, summary, rows, now):
        """Atomically store one full nearest-expiry observation, never raw tick events.

        Keep the first observation immutable on retries/restarts. Do not fill missing
        fields from another minute: NULL and stale flags preserve replay fidelity.
        """
        expiry = summary.get("selected_expiry")
        if not expiry or expiry != summary.get("expiry_selection", {}).get("nearest") or not rows:
            return
        now = local(now)
        key = (summary["index"], expiry, now.replace(second=0, microsecond=0).isoformat())
        values = []
        for row in rows:
            row = finite_json({**row, "liquidity": row.get("liquidity_state"),
                               "quote_timestamp": row.get("timestamp")})
            values.append((*key, now.isoformat(), *(row.get(field) for field in CHAIN_FIELDS)))
        columns = ("index_name", "expiry", "snapshot_minute", "timestamp", *CHAIN_FIELDS)
        with self.store.lock, self.store.db:
            # Acquire the SQLite write lock before the existence check, including
            # writers using another connection after an application restart.
            self.store.db.execute("BEGIN IMMEDIATE")
            if self.store.db.execute('''SELECT 1 FROM option_chain_snapshots
                    WHERE index_name=? AND expiry=? AND snapshot_minute=? LIMIT 1''', key).fetchone():
                return
            self.store.db.executemany(
                f"INSERT INTO option_chain_snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                values)

    def previous(self, symbol, day):
        with self.store.lock:
            row = self.store.db.execute("SELECT oi,session_date FROM option_previous_oi WHERE symbol=? AND as_of=?", (symbol, str(day))).fetchone()
            return {"oi": row[0], "session_date": row[1]} if row else None

    def save_previous(self, symbol, day, oi, session_date):
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO option_previous_oi VALUES (?,?,?,?)", (symbol, str(day), oi, session_date))

    def save(self, summary, near_rows, now):
        if not summary.get("selected_expiry"):
            return
        fields = ("index", "selected_expiry", "spot", "futures", "atm", "pcr_oi", "pcr_volume", "call_oi_wall", "put_oi_wall", "max_pain", "coverage", "stale", "last_full_chain_refresh_at")
        contract_fields = ("strike", "option_type", "trading_symbol", "ltp", "oi", "oi_change_session", "oi_change_vs_previous_close", "volume", "iv", "delta", "timestamp", "stale", "greeks_model")
        payload = {**{k: summary.get(k) for k in fields}, "contracts": [{k: r.get(k) for k in contract_fields} for r in near_rows]}
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO option_snapshots VALUES (?,?,?,?,?)",
                                  (summary["index"], summary["selected_expiry"], now.replace(second=0, microsecond=0).isoformat(), now.isoformat(), json.dumps(payload, allow_nan=False)))
