import json


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
