import csv
import io
import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

from app.signals.engine import positive

SOURCES = {"NIFTY": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
           "BANKNIFTY": "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv"}
MAJOR_BANKS = ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK")


def download(url):
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as response:
        return response.read(200_000).decode("utf-8-sig")


class ConstituentSource:
    """Current lists only. Replays must supply their own dated membership."""
    def __init__(self, path=None, fetch=download):
        self.path = path or os.getenv("BREADTH_CONSTITUENTS_FILE")
        self.fetch = fetch

    def load(self, day):
        configured = json.loads(Path(self.path).read_text(encoding="utf-8")) if self.path else {}
        result = {}
        for index, url in SOURCES.items():
            if self.path:
                item = configured.get(index, {})
                symbols = item.get("symbols", [])
                full = item.get("full_index") is True
                valid = item.get("as_of") == day.isoformat()
                weights = item.get("weights", {})
                source = "configured"
            else:
                try:
                    records = list(csv.DictReader(io.StringIO(self.fetch(url))))
                    symbols = [r["Symbol"].strip() for r in records if r.get("Series") == "EQ"]
                    if (index == "NIFTY" and len(symbols) != 50) or (index == "BANKNIFTY" and not set(MAJOR_BANKS) <= set(symbols)):
                        raise ValueError("Incomplete constituent list")
                    full = valid = True
                    weights, source = {}, "niftyindices"
                except Exception:
                    symbols = list(MAJOR_BANKS) if index == "BANKNIFTY" else []
                    weights, full, valid, source = {}, False, False, "major_banks_proxy" if symbols else "unavailable"
            if not isinstance(symbols, list) or len(symbols) != len(set(symbols)) or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9&-]+", s) for s in symbols):
                raise ValueError("Invalid constituent symbols")
            if index == "NIFTY" and len(symbols) != 50:
                full = False
            if not isinstance(weights, dict):
                raise ValueError("Invalid constituent weights")
            # Never silently combine partial weights with unit weights.
            weighted = bool(symbols) and set(weights) == set(symbols) and all(positive(v) for v in weights.values())
            result[index] = {"symbols": symbols, "weights": weights if weighted else {},
                             "full_index": full and valid, "membership_valid": valid,
                             "source": source, "as_of": day.isoformat()}
        return result


def resolve_constituents(resolver, client, universe):
    resolved = []
    for symbol in sorted({s for item in universe.values() for s in item["symbols"]}):
        rows = resolver.find(client, "NSE", tradingsymbol=symbol, segment="NSE", instrument_type="EQ")
        if len(rows) != 1:
            continue  # Missing/ambiguous metadata remains in coverage denominator.
        token = rows[0].get("instrument_token")
        if not isinstance(token, int) or isinstance(token, bool) or token <= 0 or any(r["instrument_token"] == token for r in resolved):
            continue
        resolved.append({"instrument_token": token, "trading_symbol": symbol, "friendly_name": symbol})
    return resolved
