"""Runtime adapter only. The scoring and decision engine remains pure."""
from app.signals import SignalEngine, SignalInput
from app.signals.engine import positive
from dataclasses import replace, asdict
from copy import deepcopy
from threading import Event, Thread, RLock
from queue import Empty, Queue
import logging
from .execution_models import ExecutionConfig, instant
from .planning import SignalPlanner
from .lifecycle import SignalJournal, SignalLifecycle, Submission
from .structure import completed_bars, confirmed_swings
from .explanation import safe
from .lifecycle import ACTIVE
from .decision import gate

logger = logging.getLogger('market_app')


class LiveSignals:
    def __init__(self, stream, structure, options, breadth, journal_path=':memory:', execution_config=None):
        self.stream, self.structure, self.options, self.breadth = stream, structure, options, breadth
        self.engine = SignalEngine()
        self.lifecycle = SignalLifecycle(SignalPlanner(self.engine, execution_config or ExecutionConfig.load()), SignalJournal(journal_path))
        self.stop = Event()
        self.thread = None
        self.tick_thread = None
        self.tick_queue = None
        self.view_lock = RLock()
        self.views = {}

    def start(self):
        if self.thread or self.stop.is_set():
            return
        self.tick_queue = self.stream.state.subscribe(maxsize=10000)
        if isinstance(self.tick_queue, Queue):
            self.tick_thread = Thread(target=self._watch_ticks, daemon=True, name='signal-trigger-watch')
            self.tick_thread.start()
        self.thread = Thread(target=self._run, daemon=True, name='signal-observer')
        self.thread.start()

    def _watch_ticks(self):
        while not self.stop.is_set():
            try:
                tick = self.tick_queue.get(timeout=.5)
                self.lifecycle.observe_tick(tick.symbol, tick.last_price, tick.timestamp, tick.received_at)
            except Empty:
                pass
            except Exception:
                logger.warning('', extra={'event': 'signal_trigger_watch_unavailable'})

    def _run(self):
        while not self.stop.is_set():
            for index in ('NIFTY', 'BANKNIFTY'):
                if self.stop.is_set():
                    break
                try:
                    self.evaluate(index)
                except Exception:
                    with self.view_lock:
                        self.views.pop(index, None)
                    logger.warning('', extra={'event': 'signal_observation_unavailable'})
            self.stop.wait(2)

    def current(self, index):
        with self.view_lock:
            result = deepcopy(self.views.get(index))
        if result and 0 <= (self.stream.state.clock()-instant(result['last_updated'])).total_seconds() <= 10:
            return result
        missing = asdict(self.engine.decide(SignalInput(index, self.stream.state.clock().isoformat())))
        return {**missing, 'index': index, 'decision': 'NO_TRADE', 'state': None, 'confidence': 0, 'record': None,
                'evidence': [], 'contradictions': [], 'last_updated': result['last_updated'] if result else None,
                'qualification_gates': [*missing['qualification_gates'],
                    self.lifecycle.planner.entry_window_gate(self.stream.state.clock().isoformat()),
                    gate('observation', 'Signal observation', False, detail='Waiting for fresh signal observations')],
                'data_quality': {'stale': True, 'blocking_reasons': ['Waiting for fresh signal observations']}}

    def history(self, **filters):
        with self.lifecycle.lock:
            return self.lifecycle.journal.history(**filters)

    def detail(self, signal_id):
        with self.lifecycle.lock:
            return self.lifecycle.journal.detail(signal_id)

    def _publish(self, index, snapshot, submission):
        decision = self.engine.decide(snapshot)
        scored = asdict(decision)
        record = submission.record
        active = record and record.state in ACTIVE
        original = None
        if active:
            with self.lifecycle.lock:
                detail = self.lifecycle.journal.detail(record.signal_id)
            original = (detail or {}).get('creation_explanation')
        original_scores = (original or {}).get('scores', {})
        quality = dict(scored['data_quality'])
        quality['planning_reasons'] = list(submission.reasons)
        quality['stale'] = any(not quality.get(k, False) for k in ('structure_fresh', 'options_signal_data', 'breadth_fresh', 'vix_fresh'))
        gates = [*scored['qualification_gates'], self.lifecycle.planner.entry_window_gate(snapshot.as_of),
                 gate('planning', 'Signal planning', True if submission.created else None if active else False,
                      detail='; '.join(submission.reasons) or 'Qualified candidate created')]
        result = {**scored, 'index': index, 'decision': record.plan.direction if active else 'NO_TRADE',
                  'state': record.state if record else None, 'record': asdict(record) if record else None,
                  'data_quality': quality, 'last_updated': snapshot.as_of, 'qualification_gates': gates}
        if active and original_scores:
            for field in ('confidence', 'bullish_score', 'bearish_score', 'category_scores', 'evidence', 'contradictions'):
                if field in original_scores:
                    result[field] = original_scores[field]
        result['current_qualification'] = scored
        # Keep creation scores and their expiry together; today's gates and
        # analysis metadata always describe the current observation separately.
        result['score_expiry_selection'] = ((original or {}).get('input', {}).get('options', {}).get('expiry_selection', {})
                                             if active and original_scores else scored['expiry_selection'])
        result['score_basis'] = 'candidate_creation' if active and original_scores else 'current_observation'
        if not active:
            result['confidence'] = 0
        with self.view_lock:
            self.views[index] = safe(result)
        return submission

    def snapshot(self, index, option_summary=None):
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
        options = option_summary if option_summary is not None else self.options.response(index)[0]
        breadth = self.breadth.snapshot(index)
        return SignalInput(index, self.stream.state.clock().isoformat(), structure, options, volatility, breadth)

    def decision(self, index):
        return self.engine.decide(self.snapshot(index))

    def evaluate(self, index):
        """Explicit internal call only; no polling thread or order integration."""
        summary, contracts = self.options.response(index)
        snapshot = self.snapshot(index, option_summary=summary)
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
            minute_bars = self.structure.aggregator.candles(reference, '1m', 10)
            record = self.lifecycle.advance(active.signal_id, snapshot, contracts, rows[-1] if rows else None, minute_bars)
            return self._publish(index, snapshot, Submission('NO_TRADE', record, False, ('Existing signal lifecycle observed; no new signal',)))
        return self._publish(index, snapshot, self.lifecycle.submit(snapshot, contracts))

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError('Signal observer shutdown timed out')
        if self.tick_thread:
            self.tick_thread.join(timeout=10)
            if self.tick_thread.is_alive():
                raise RuntimeError('Signal trigger watcher shutdown timed out')
        if isinstance(self.tick_queue, Queue):
            self.stream.state.unsubscribe(self.tick_queue)
        self.lifecycle.close()
