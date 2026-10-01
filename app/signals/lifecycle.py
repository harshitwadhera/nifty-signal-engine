"""Serialized, replayable observation state machine. No orders or assumed fills."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from threading import RLock
from contextlib import contextmanager, nullcontext

from .decision import fresh
from .engine import positive
from .execution_models import (EntryTrigger, LifecycleEvent, SelectedOption, SignalPlan,
                               SignalRecord, StructuralLevel, instant)
from .planning import SignalPlanner, risk_reward
from .selection import select_option
from .explanation import explain, safe
from .outcomes import entered, observe, transitioned

ACTIVE = {'CANDIDATE', 'EARLY_SETUP', 'CONFIRMED', 'TARGET1_HIT'}


class SignalJournal:
    """Optional durable idempotency/history, sharing the ignored market DB path."""
    def __init__(self, path=':memory:'):
        self.transaction_depth = 0
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS signal_lifecycle (
            signal_id TEXT PRIMARY KEY, index_name TEXT NOT NULL,
            session_date TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)''')
        self.db.execute('''CREATE INDEX IF NOT EXISTS signal_lifecycle_session
            ON signal_lifecycle(session_date, index_name, state)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY, index_name TEXT NOT NULL, direction TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, state TEXT NOT NULL,
            record TEXT NOT NULL)''')
        self.db.execute('''CREATE INDEX IF NOT EXISTS signals_history
            ON signals(created_at DESC, signal_id DESC)''')
        self.db.execute('''CREATE INDEX IF NOT EXISTS signals_filter
            ON signals(index_name, state, direction, created_at DESC)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS signal_events (
            signal_id TEXT NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL,
            observed_at TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(signal_id, sequence))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS signal_outcomes (
            signal_id TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
        # Existing Phase 5.4 records are visible, with missing historical evidence
        # explicitly NULL. Never attach today's snapshot to yesterday's signal.
        self.db.execute('''INSERT OR IGNORE INTO signals
            SELECT signal_id, index_name, json_extract(payload, '$.plan.direction'),
                   json_extract(payload, '$.created_at'), json_extract(payload, '$.updated_at'), state, payload
            FROM signal_lifecycle''')
        for signal_id, payload in self.db.execute('SELECT signal_id, payload FROM signal_lifecycle').fetchall():
            legacy = json.loads(payload)
            self.db.execute('INSERT OR IGNORE INTO signal_outcomes VALUES (?,?)',
                            (signal_id, json.dumps(legacy.get('outcome', {}), allow_nan=False)))
            history = legacy.get('history')
            for sequence, event in enumerate(history if isinstance(history, list) else []):
                if not (isinstance(event, dict) and isinstance(event.get('state'), str) and isinstance(event.get('at'), str)):
                    continue
                self.db.execute('INSERT OR IGNORE INTO signal_events VALUES (?,?,?,?,?)',
                    (signal_id, sequence, event['state'], event['at'],
                     json.dumps({'event': event, 'record': None, 'explanation': None}, allow_nan=False)))
        self.db.commit()

    @contextmanager
    def transaction(self):
        if self.transaction_depth:
            yield
            return
        self.transaction_depth += 1
        try:
            with self.db:
                yield
        finally:
            self.transaction_depth -= 1

    def save(self, record, context=None):
        payload = json.dumps(safe(asdict(record)), allow_nan=False)
        with (nullcontext() if self.transaction_depth else self.db):
            self.db.execute('INSERT OR REPLACE INTO signal_lifecycle VALUES (?,?,?,?,?)',
                (record.signal_id, record.plan.index_name, instant(record.created_at).date().isoformat(),
                 record.state, payload))
            self.db.execute('INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?)',
                (record.signal_id, record.plan.index_name, record.plan.direction, record.created_at,
                 record.updated_at, record.state, payload))
            self.db.execute('INSERT OR REPLACE INTO signal_outcomes VALUES (?,?)',
                            (record.signal_id, json.dumps(safe(record.outcome), allow_nan=False)))
            for sequence, event in enumerate(record.history):
                evidence = context if context and instant(context['as_of']) == instant(event.at) else None
                event_payload = {'event': asdict(event), 'record': asdict(record) if evidence else None,
                                 'explanation': evidence}
                self.db.execute('INSERT OR IGNORE INTO signal_events VALUES (?,?,?,?,?)',
                    (record.signal_id, sequence, event.state, event.at, json.dumps(safe(event_payload), allow_nan=False)))

    def detail(self, signal_id):
        row = self.db.execute('SELECT record FROM signals WHERE signal_id=?', (signal_id,)).fetchone()
        if not row:
            return None
        events = [json.loads(r[0]) for r in self.db.execute(
            'SELECT payload FROM signal_events WHERE signal_id=? ORDER BY sequence', (signal_id,))]
        return {'record': json.loads(row[0]), 'events': events,
                'creation_explanation': events[0]['explanation'] if events else None}

    def history(self, limit=25, offset=0, index=None, state=None, direction=None, date_from=None, date_to=None,
                confirmed_only=False):
        conditions, values = [], []
        for column, value in (('index_name', index), ('state', state), ('direction', direction)):
            if value is not None:
                conditions.append(column+'=?')
                values.append(value)
        for op, value in (('>=', date_from), ('<=', date_to)):
            if value is not None:
                conditions.append('substr(created_at,1,10)'+op+'?')
                values.append(str(value))
        source = 'signals'
        if confirmed_only:
            source += """ JOIN (
                SELECT signal_id, MIN(observed_at) AS confirmed_at FROM signal_events
                WHERE state='CONFIRMED' GROUP BY signal_id
            ) confirmations USING (signal_id)"""
        where = ' WHERE '+' AND '.join(conditions) if conditions else ''
        total = self.db.execute('SELECT count(*) FROM '+source+where, values).fetchone()[0]
        if not confirmed_only:
            rows = self.db.execute('SELECT record FROM signals'+where+' ORDER BY created_at DESC, signal_id DESC LIMIT ? OFFSET ?',
                                  [*values, limit, offset]).fetchall()
            items = [json.loads(r[0]) for r in rows]
        else:
            rows = self.db.execute('SELECT record, signal_id, index_name, direction, state, confirmed_at FROM '+source+where+
                                   ' ORDER BY confirmed_at DESC, signal_id DESC LIMIT ? OFFSET ?',
                                   [*values, limit, offset]).fetchall()
            events = {row[1]: [] for row in rows}
            if events:
                placeholders = ','.join('?' for _ in events)
                for signal_id, state, at in self.db.execute(
                        f'SELECT signal_id, state, observed_at FROM signal_events WHERE signal_id IN ({placeholders}) '
                        'ORDER BY signal_id, sequence', tuple(events)):
                    events[signal_id].append({'state': state, 'at': at})
            items = []
            for payload, signal_id, index_name, direction, state, confirmed_at in rows:
                try:
                    record = json.loads(payload)
                except (ValueError, TypeError):
                    record = {}
                record = record if isinstance(record, dict) else {}
                plan = record.get('plan')
                plan = dict(plan) if isinstance(plan, dict) else {}
                plan.update(index_name=index_name, direction=direction)
                # Event rows are authoritative even when serialized history is
                # incomplete. This projection never writes back to either journal.
                items.append({**record, 'signal_id': signal_id, 'state': state, 'plan': plan,
                              'confirmed_at': confirmed_at, 'history': events[signal_id]})
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

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
        self.context = None

    @contextmanager
    def _operation(self):
        with self.lock:
            try:
                with self.journal.transaction():
                    yield
            except Exception:
                # Restore the memory mirror as well as rolling back SQLite.
                self.records = self.journal.load()
                raise

    def active(self, index):
        with self.lock:
            return next((r for r in self.records.values() if r.plan.index_name == index and r.state in ACTIVE), None)

    def _save(self, record):
        self.journal.save(record, self.context)
        self.records[record.signal_id] = record
        return record

    def _transition(self, record, state, now, reason, **changes):
        changes.setdefault('outcome', transitioned(record, state, now))
        return self._save(replace(record, state=state, updated_at=now.isoformat(),
            history=(*record.history, LifecycleEvent(state, now.isoformat(), reason)), **changes))

    def submit(self, snapshot, contracts):
        now = instant(snapshot.as_of)
        with self._operation():
            self.context = explain(snapshot, contracts, self.planner)
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

    def observe_tick(self, symbol, price, at):
        """Persist the first live trigger breach; a tick alone never confirms."""
        try:
            now, price = instant(at), positive(price)
        except (TypeError, ValueError):
            return None
        if price is None:
            return None
        with self._operation():
            record = next((r for r in self.records.values()
                if r.state in ('CANDIDATE', 'EARLY_SETUP') and r.plan.entry_trigger.instrument == symbol), None)
            if record is None or now < instant(record.created_at):
                return record
            watch = dict(record.trigger_watch or {})
            previous_tick = watch.get('last_tick_at')
            if previous_tick:
                try:
                    if now <= instant(previous_tick):
                        return record
                except ValueError:
                    pass
            watch['last_tick_at'] = now.isoformat()
            level = record.plan.entry_trigger.level
            sign = 1 if record.plan.direction == 'CALL' else -1
            if sign*(price-level) <= 0:
                return self._save(replace(record, trigger_watch=watch))
            if watch.get('breached_at'):
                return self._save(replace(record, trigger_watch=watch))
            watch.update({
                'signal_id': record.signal_id,
                'direction': record.plan.direction,
                'trigger_price': level,
                'breached_at': now.isoformat(),
                'breach_price': price,
                'underlying_symbol': symbol,
                'selected_option': record.plan.option.trading_symbol,
                'lifecycle_state': record.state,
                'structure_1m': None,
            })
            return self._save(replace(record, trigger_watch=watch))

    def _early_structure(self, record, bars, now):
        """Use only completed 1m bars after a live breach; no percentage threshold."""
        watch = record.trigger_watch or {}
        if not watch.get('breached_at'):
            return None
        try:
            breached = instant(watch['breached_at'])
        except (TypeError, ValueError):
            return None
        valid = []
        for bar in bars or ():
            if not isinstance(bar, dict) or bar.get('completed') is not True or bar.get('partial') is not False:
                continue
            if bar.get('symbol') != record.plan.entry_trigger.instrument or bar.get('interval') != '1m':
                continue
            try:
                start, end = instant(bar['start_time']), instant(bar['end_time'])
                if end-start != timedelta(minutes=1) or end <= breached or end > now:
                    continue
                if not 0 <= (now-end).total_seconds() <= self.config.observation_max_age_seconds+60:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            opened, high, low, close = [positive(bar.get(k)) for k in ('open', 'high', 'low', 'close')]
            if any(v is None for v in (opened, high, low, close)) or not low <= min(opened, close) <= max(opened, close) <= high:
                continue
            valid.append((end, opened, high, low, close))
        valid.sort(key=lambda row: row[0])
        if not valid:
            return None
        level = record.plan.entry_trigger.level
        sign = 1 if record.plan.direction == 'CALL' else -1
        latest = valid[-1]
        if sign*(latest[4]-level) <= 0:
            return {'failed': True, 'reason': 'Completed 1m candle recovered through the trigger'}
        if len(valid) < 2:
            return {'qualified': False, 'failed': False, 'reason': 'Waiting for a second completed 1m structure candle'}
        previous = valid[-2]
        if sign*(previous[4]-level) <= 0:
            return {'qualified': False, 'failed': False, 'reason': 'First completed 1m candle did not hold beyond the trigger'}
        directional_body = sign*(latest[4]-latest[1]) > 0
        resumed = sign*(latest[4]-previous[4]) > 0
        if sign == 1:
            retest_rejection = latest[3] <= level <= latest[2] and latest[4] > level
            continuation = latest[3] >= previous[3] and latest[2] > previous[2]
            direction = 'BULLISH'
        else:
            retest_rejection = latest[3] <= level <= latest[2] and latest[4] < level
            continuation = latest[2] <= previous[2] and latest[3] < previous[3]
            direction = 'BEARISH'
        qualified = directional_body and resumed and (retest_rejection or continuation)
        return {
            'qualified': qualified,
            'failed': False,
            'direction': direction if qualified else 'DEVELOPING',
            'retest_rejection': retest_rejection,
            'continuation_structure': continuation,
            'latest_close': latest[4],
            'latest_end': latest[0].isoformat(),
            'reason': ('Completed 1m retest/rejection and continuation' if retest_rejection and qualified
                       else 'Two completed 1m candles show directional continuation' if qualified
                       else '1m follow-through is not yet strong enough'),
        }

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

    def advance(self, signal_id, snapshot, contracts=(), bar=None, minute_bars=()):
        now = instant(snapshot.as_of)
        with self._operation():
            self.context = explain(snapshot, contracts, self.planner, bar)
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
            stop_touched = sign*(adverse-plan.invalidation.level) <= 0
            if record.state in ('CONFIRMED', 'TARGET1_HIT'):
                record = self._save(replace(record, outcome=observe(record, price, favorable, adverse, now, stop_touched)))
            # A bar can touch both stop and target without revealing ordering.
            # Conservatively invalidate/stop first; never infer a profitable fill.
            if sign*(adverse-plan.invalidation.level) <= 0:
                state = 'INVALIDATED' if record.state in ('CANDIDATE', 'EARLY_SETUP') else 'STOPPED'
                return self._transition(record, state, now, 'Underlying structural invalidation touched (stop-first for ambiguous bars)')
            if record.state in ('CANDIDATE', 'EARLY_SETUP'):
                level = plan.entry_trigger.level
                early = self._early_structure(record, minute_bars, now)
                if early and early.get('failed'):
                    if record.state == 'EARLY_SETUP':
                        return self._transition(record, 'CANDIDATE', now,
                            'Early setup failed before 5m confirmation; waiting for a new live breach', trigger_watch={})
                    record = self._save(replace(record, trigger_watch={}))
                elif early:
                    watch = dict(record.trigger_watch or {})
                    watch['structure_1m'] = early
                    record = self._save(replace(record, trigger_watch=watch))
                    if (record.state == 'CANDIDATE' and early.get('qualified')
                            and self.planner.engine.decide(snapshot).decision == plan.direction):
                        record = self._transition(record, 'EARLY_SETUP', now,
                            'Live trigger breach plus completed 1m continuation/retest; 5m confirmation pending',
                            trigger_watch=watch)
                if not values:
                    return record
                opened, high, low, close = values
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
                                        outcome=entered(entry, next((positive(r.get('ltp')) for r in contracts
                                            if r.get('instrument_token') == selected.instrument_token), None), now),
                                        confirmed_t2_rr=risk_reward(plan.direction, entry, plan.invalidation.level, plan.target2.level) if plan.target2 else None)
            if sign*(favorable-plan.target1.level) >= 0 and record.state == 'CONFIRMED':
                record = self._transition(record, 'TARGET1_HIT', now, 'Underlying target 1 observed')
            if plan.target2 and sign*(favorable-plan.target2.level) >= 0:
                return self._transition(record, 'TARGET2_HIT', now, 'Underlying target 2 observed')
            return record

    def close(self):
        with self.lock:
            self.journal.close()
