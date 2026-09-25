"""Runtime adapter only. The scoring and decision engine remains pure."""
from app.signals import SignalEngine, SignalInput
from app.signals.engine import positive


class LiveSignals:
    def __init__(self, stream, structure, options, breadth):
        self.stream, self.structure, self.options, self.breadth = stream, structure, options, breadth
        self.engine = SignalEngine()

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
