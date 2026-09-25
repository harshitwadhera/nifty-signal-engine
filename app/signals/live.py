"""Runtime adapter only. The scoring and decision engine remains pure."""
from app.signals import SignalEngine, SignalInput
from app.signals.engine import positive
from dataclasses import replace
from .execution_models import ExecutionConfig, instant
from .planning import SignalPlanner
from .lifecycle import SignalJournal, SignalLifecycle, Submission
from .structure import completed_bars, confirmed_swings


class LiveSignals:
    def __init__(self, stream, structure, options, breadth, journal_path=':memory:', execution_config=None):
        self.stream, self.structure, self.options, self.breadth = stream, structure, options, breadth
        self.engine = SignalEngine()
        self.lifecycle = SignalLifecycle(SignalPlanner(self.engine, execution_config or ExecutionConfig.load()), SignalJournal(journal_path))

    def snapshot(self, index):
        if index not in ("NIFTY", "BANKNIFTY"):
            raise ValueError("Unsupported index")
        market = self.structure.structure()
        structure = dict(market[index.lower()], as_of=market["as_of"])
        rows = self.stream.live()["instruments"]
        vix = next((r for r in rows if r["trading_symbol"] == "INDIA VIX"), {})
        def change(row):
            price, close = positive(row.get("last_price")), positive((row.get("ohlc") or {}).get("close"))
            return (price/close-1)*100 if price is not None and close is not None else None
        volatility = {"level": vix.get("last_price"), "change_percent": change(vix),
                      "stale": vix.get("stale", True), "as_of": vix.get("last_tick_received_at")}
        spot = next((r for r in rows if r["trading_symbol"] == ("NIFTY 50" if index == "NIFTY" else "NIFTY BANK")), {})
        future = next((r for r in rows if r.get("alias") == index+"_FUT"), {})
        structure.update(spot_change_percent=change(spot), future_change_percent=change(future))
        if spot.get("stale", True) or future.get("stale", True):
            structure["stale"] = True
        options, _ = self.options.response(index)
        breadth = self.breadth.snapshot(index)
        return SignalInput(index, self.stream.state.clock().isoformat(), structure, options, volatility, breadth)

    def decision(self, index):
        return self.engine.decide(self.snapshot(index))

    def evaluate(self, index):
        """Explicit internal call only; no polling thread or order integration."""
        snapshot = self.snapshot(index)
        summary, contracts = self.options.response(index)
        now = self.stream.state.clock().isoformat()
        structure = dict(snapshot.structure)
        symbol = 'NIFTY 50' if index == 'NIFTY' else 'NIFTY BANK'
        bars = self.structure.aggregator.candles(symbol, '5m', 100)
        structure.update(confirmed_swings(bars, now, symbol))
        future_symbol = structure.get('future_symbol')
        future_bars = []
        if future_symbol:
            future_bars = self.structure.aggregator.candles(future_symbol, '5m', 100)
            previous = self.structure.store.previous(future_symbol, instant(now).date()) or {}
            future_structure = dict(symbol=future_symbol, price=structure.get('future'), vwap=structure.get('futures_vwap'),
                                    previous_day_high=previous.get('high'), previous_day_low=previous.get('low'))
            future_structure.update(confirmed_swings(future_bars, now, future_symbol))
            structure['future_structure'] = future_structure
        snapshot = replace(snapshot, as_of=now, structure=structure, options=summary)
        active = self.lifecycle.active(index)
        if active:
            reference = active.plan.entry_trigger.instrument
            rows = completed_bars(bars if reference == symbol else future_bars, now, reference)
            record = self.lifecycle.advance(active.signal_id, snapshot, contracts, rows[-1] if rows else None)
            return Submission('NO_TRADE', record, False, ('Existing signal lifecycle observed; no new signal',))
        return self.lifecycle.submit(snapshot, contracts)

    def close(self):
        self.lifecycle.close()
