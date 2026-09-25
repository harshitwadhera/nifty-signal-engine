"""Serialized, replayable observation state machine. No orders or assumed fills."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from threading import RLock

from .decision import fresh
from .engine import positive
from .execution_models import (EntryTrigger, LifecycleEvent, SelectedOption, SignalPlan,
                               SignalRecord, StructuralLevel, instant)
from .planning import SignalPlanner, risk_reward
from .selection import select_option

ACTIVE = {'CANDIDATE', 'CONFIRMED', 'TARGET1_HIT'}


class SignalJournal:
    """Optional durable idempotency/history, sharing the ignored market DB path."""
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS signal_lifecycle (
            signal_id TEXT PRIMARY KEY, index_name TEXT NOT NULL,
            session_date TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)''')
        self.db.execute('''CREATE INDEX IF NOT EXISTS signal_lifecycle_session
            ON signal_lifecycle(session_date, index_name, state)''')
        self.db.commit()

    def save(self, record):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO signal_lifecycle VALUES (?,?,?,?,?)',
                (record.signal_id, record.plan.index_name, instant(record.created_at).date().isoformat(),
                 record.state, json.dumps(asdict(record), allow_nan=False)))

    def load(self):
        records = {}
        for payload, in self.db.execute('SELECT payload FROM signal_lifecycle'):
            row = json.loads(payload)
            plan = row['plan']
            plan['entry_trigger'] = EntryTrigger(**plan['entry_trigger'])
            for key in ('invalidation', 'target1', 'target2'):
                plan[key] = StructuralLevel(**plan[key]) if plan[key] else None
            plan['option'] = SelectedOption(**plan['option'])
            row['plan'] = SignalPlan(**plan)
            row['history'] = tuple(LifecycleEvent(**event) for event in row['history'])
            records[row['signal_id']] = SignalRecord(**row)
        return records

    def close(self):
        self.db.close()


@dataclass(frozen=True)
class Submission:
    decision: str
    record: SignalRecord | None
    created: bool
    reasons: tuple[str, ...]


class SignalLifecycle:
    def __init__(self, planner=None, journal=None):
        self.planner = planner or SignalPlanner()
        self.config = self.planner.config
        self.journal = journal or SignalJournal()
        self.records = self.journal.load()
        self.lock = RLock()

    def active(self, index):
        with self.lock:
            return next((r for r in self.records.values() if r.plan.index_name == index and r.state in ACTIVE), None)

    def _save(self, record):
        self.journal.save(record)
        self.records[record.signal_id] = record
        return record

    def _transition(self, record, state, now, reason, **changes):
        return self._save(replace(record, state=state, updated_at=now.isoformat(),
            history=(*record.history, LifecycleEvent(state, now.isoformat(), reason)), **changes))

    def submit(self, snapshot, contracts):
        now = instant(snapshot.as_of)
        with self.lock:
            existing = self.active(snapshot.index_name)
            if existing:
                self.advance(existing.signal_id, snapshot, contracts)
                existing = self.active(snapshot.index_name)
                if existing:
                    return Submission('NO_TRADE', existing, False, ('An active signal already exists for this index',))
            if any(r.plan.index_name == snapshot.index_name and instant(r.updated_at) > now for r in self.records.values()):
                return Submission('NO_TRADE', None, False, ('Out-of-order observation',))
            result = self.planner.build(snapshot, contracts)
            if not result.actionable:
                return Submission('NO_TRADE', None, False, result.reasons)
            plan = result.plan
            # Quotes/targets/candidate timestamps cannot manufacture a new ID for
            # the same structural setup. No same-session resurrection after exit.
            fingerprint = (snapshot.index_name, now.date().isoformat(), plan.direction,
                           plan.entry_trigger.instrument, plan.entry_trigger.source, plan.entry_trigger.level)
            signal_id = sha256(json.dumps(fingerprint).encode()).hexdigest()[:24]
            if signal_id in self.records:
                return Submission('NO_TRADE', self.records[signal_id], False, ('Duplicate structural setup',))
            expiry = min(now+timedelta(seconds=self.config.candidate_lifetime_seconds),
                         datetime.combine(now.date(), self.config.new_entry_cutoff, now.tzinfo))
            record = SignalRecord(signal_id, 'CANDIDATE', plan, now.isoformat(), expiry.isoformat(), now.isoformat(),
                                  history=(LifecycleEvent('CANDIDATE', now.isoformat(), 'Direction, structure and liquidity qualify'),))
            return Submission(plan.direction, self._save(record), True, ())

    def _bar(self, record, bar, now):
        if not isinstance(bar, dict) or bar.get('completed') is not True or bar.get('partial') is not False:
            return None
        if bar.get('symbol') != record.plan.entry_trigger.instrument or bar.get('interval') != '5m':
            return None
        try:
            start, end = instant(bar['start_time']), instant(bar['end_time'])
            confirmed = next((instant(e.at) for e in record.history if e.state == 'CONFIRMED'), instant(record.created_at))
            if (end-start != timedelta(minutes=5) or start < confirmed or
                    (record.last_bar_end is not None and end <= instant(record.last_bar_end)) or
                    not 0 <= (now-end).total_seconds() <= self.config.observation_max_age_seconds):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        values = [positive(bar.get(k)) for k in ('open', 'high', 'low', 'close')]
        if any(v is None for v in values):
            return None
        opened, high, low, close = values
        if not low <= min(opened, close) <= max(opened, close) <= high:
            return None
        return opened, high, low, close

    def advance(self, signal_id, snapshot, contracts=(), bar=None):
        now = instant(snapshot.as_of)
        with self.lock:
            record = self.records[signal_id]
            if snapshot.index_name != record.plan.index_name:
                raise ValueError('Signal index mismatch')
            if record.state not in ACTIVE or now <= instant(record.updated_at):
                return record
            created = instant(record.created_at)
            if now.date() != created.date() or now.time() >= self.config.session_end:
                return self._transition(record, 'EXPIRED', now, 'Session ended')
            if record.state == 'CANDIDATE' and now >= instant(record.expires_at):
                return self._transition(record, 'EXPIRED', now, 'Candidate lifetime or new-entry cutoff reached')
            # Persist the observation watermark even when there is no transition.
            # Replay of an older snapshot cannot later change the signal.
            record = self._save(replace(record, updated_at=now.isoformat()))
            if not fresh(snapshot.structure, 'as_of', snapshot.as_of, self.config.observation_max_age_seconds):
                return record
            plan = record.plan
            is_spot = plan.entry_trigger.instrument in ('NIFTY 50', 'NIFTY BANK')
            if not is_spot and snapshot.structure.get('future_symbol') != plan.entry_trigger.instrument:
                return self._transition(record, 'INVALIDATED' if record.state == 'CANDIDATE' else 'EXPIRED', now, 'Underlying contract changed')
            price = positive(snapshot.structure.get('spot' if is_spot else 'future'))
            if price is None:
                return record
            values = self._bar(record, bar, now)
            if values:
                record = self._save(replace(record, last_bar_end=instant(bar['end_time']).isoformat()))
            high, low = (values[1], values[2]) if values else (price, price)
            sign = 1 if plan.direction == 'CALL' else -1
            adverse = min(low, price) if sign == 1 else max(high, price)
            favorable = max(high, price) if sign == 1 else min(low, price)
            # A bar can touch both stop and target without revealing ordering.
            # Conservatively invalidate/stop first; never infer a profitable fill.
            if sign*(adverse-plan.invalidation.level) <= 0:
                state = 'INVALIDATED' if record.state == 'CANDIDATE' else 'STOPPED'
                return self._transition(record, state, now, 'Underlying structural invalidation touched (stop-first for ambiguous bars)')
            if record.state == 'CANDIDATE':
                if not values:
                    return record
                opened, high, low, close = values
                level = plan.entry_trigger.level
                crossed = sign*(opened-level) <= 0 and sign*(close-level) > 0
                if plan.entry_trigger.type == 'breakout_retest':
                    crossed = sign*(opened-level) > 0 and low <= level <= high and sign*(close-level) > 0
                if not crossed:
                    return record
                if self.planner.engine.decide(snapshot).decision != plan.direction:
                    return record
                # Re-select relative to current spot; an old ATM that drifted far
                # OTM is never silently retained at confirmation.
                selected = select_option(snapshot, plan.direction, contracts, self.config)
                if selected is None or selected.instrument_token != plan.option.instrument_token:
                    return self._transition(record, 'INVALIDATED', now, 'Selected contract no longer eligible at confirmation')
                # Do not use the old trigger price to hide a gap/chase. Use the
                # worse of close/current observation for conservative observed R:R.
                entry = max(close, price) if sign == 1 else min(close, price)
                rr = risk_reward(plan.direction, entry, plan.invalidation.level, plan.target1.level)
                if rr is None or rr < self.config.minimum_t1_rr:
                    return self._transition(record, 'INVALIDATED', now, 'Confirmation risk/reward below minimum')
                return self._transition(record, 'CONFIRMED', now, 'Completed 5m candle and fresh qualification confirmed',
                                        confirmation_price=entry, confirmed_t1_rr=rr,
                                        confirmed_t2_rr=risk_reward(plan.direction, entry, plan.invalidation.level, plan.target2.level) if plan.target2 else None)
            if sign*(favorable-plan.target1.level) >= 0 and record.state == 'CONFIRMED':
                record = self._transition(record, 'TARGET1_HIT', now, 'Underlying target 1 observed')
            if plan.target2 and sign*(favorable-plan.target2.level) >= 0:
                return self._transition(record, 'TARGET2_HIT', now, 'Underlying target 2 observed')
            return record

    def close(self):
        with self.lock:
            self.journal.close()
