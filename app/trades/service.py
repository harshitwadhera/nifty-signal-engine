"""Manual journal orchestration using the existing index tick bus and option cache."""
from datetime import datetime, timezone
from queue import Empty
from threading import Event, RLock, Thread
from uuid import uuid4
import logging

from app.signals.execution_models import instant
from .monitor import SYMBOLS, fresh, observe, price
from .storage import TradeJournal

logger = logging.getLogger('market_app')


class TradeService:
    def __init__(self, stream, signals, options, path=':memory:', clock=None):
        self.stream, self.signals, self.options = stream, signals, options
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.journal = TradeJournal(path)
        self.lock = RLock()
        self.stop = Event()
        self.thread = None
        self.queue = None
        self.last_monitor_at = None

    def market(self, index):
        live = self.stream.live()
        row = next((r for r in live['instruments'] if r.get('trading_symbol') == SYMBOLS[index]), {})
        return row, live.get('websocket_status') == 'connected'

    def contract(self, index, option):
        try:
            _, rows = self.options.response(index, instant(option['expiry']+'T00:00:00+05:30').date())
            return next((r for r in rows if r.get('instrument_token') == option['instrument_token']
                         and r.get('trading_symbol') == option['trading_symbol']), {})
        except (ValueError, KeyError, TypeError):
            return {}

    def quote(self, contract):
        row = {'last_price': contract.get('ltp'), 'stale': contract.get('stale', True),
               'last_tick_received_at': contract.get('received_at'),
               'last_exchange_timestamp': contract.get('timestamp')}
        return contract.get('ltp') if fresh(row, self.clock()) else None

    def setup(self, index):
        view = self.signals.current(index)
        record = view.get('record') or {}
        plan = record.get('plan') or {}
        option = plan.get('option') or {}
        contract = self.contract(index, option) if option else {}
        row, connected = self.market(index)
        now = self.clock()
        current = row.get('last_price') if fresh(row, now, connected) else None
        lot = contract.get('lot_size')
        valid_lot = isinstance(lot, int) and not isinstance(lot, bool) and lot > 0
        coordinate_ok = (plan.get('entry_trigger') or {}).get('instrument') == SYMBOLS[index]
        state = 'WAITING' if record.get('state') == 'CANDIDATE' else 'READY' if record.get('state') == 'CONFIRMED' else 'NO_TRADE'
        reason = None
        if state == 'READY':
            if not coordinate_ok:
                reason = 'This signal uses futures levels; an index-based stop is required for manual tracking.'
            elif current is None or self.quote(contract) is None or view.get('data_quality', {}).get('stale', True):
                reason = 'Waiting for fresh underlying, option and signal data.'
            elif not valid_lot:
                reason = 'Contract lot size unavailable.'
            elif instant(option['expiry']+'T15:30:00+05:30') <= instant(now):
                reason = 'Option contract has expired.'
        return {'index': index, 'setup_state': state, 'can_confirm': state == 'READY' and reason is None,
                'reason': reason, 'signal': view, 'lot_size': lot if valid_lot else None,
                'option_ltp': self.quote(contract), 'underlying_current': current,
                'fresh': current is not None, 'server_time': instant(now).isoformat(),
                'index_levels': coordinate_ok}

    def confirm(self, signal_id, body):
        with self.lock:
            detail = self.signals.detail(signal_id)
            if not detail:
                raise KeyError('Signal not found')
            record = detail['record']
            plan = record['plan']
            setup = self.setup(plan['index_name'])
            if record['state'] != 'CONFIRMED' or not setup['can_confirm'] or (setup['signal'].get('record') or {}).get('signal_id') != signal_id:
                raise ValueError(setup.get('reason') or 'A current CONFIRMED signal is required.')
            if body.quantity != body.lots*setup['lot_size']:
                raise ValueError('Quantity must equal lots multiplied by the actual contract lot size.')
            now, opened = instant(self.clock()), instant(body.opened_at)
            confirmation_time = record.get('outcome', {}).get('entry_time') or record['updated_at']
            if not instant(confirmation_time) <= opened <= now or (now-opened).total_seconds() > 300:
                raise ValueError('Entry time must be after confirmation and within the last five minutes.')
            sign = 1 if plan['direction'] == 'CALL' else -1
            stop, target = plan['invalidation']['level'], plan['target1']['level']
            if sign*(body.underlying_entry-stop) <= 0 or sign*(target-body.underlying_entry) <= 0:
                raise ValueError('Underlying entry must lie between the structural stop and first target.')
            if sign*(setup['underlying_current']-stop) <= 0 or sign*(target-setup['underlying_current']) <= 0:
                raise ValueError('Underlying has already reached the stop or first target; refresh the setup.')
            option = plan['option']
            trade = dict(trade_id=uuid4().hex, signal_id=signal_id, index_name=plan['index_name'],
                direction=plan['direction'], option_symbol=option['trading_symbol'], instrument_token=option['instrument_token'],
                option_type=option['option_type'], strike=option['strike'], expiry=option['expiry'],
                quantity=body.quantity, lots=body.lots, lot_size=setup['lot_size'], actual_entry_premium=body.actual_entry_premium,
                underlying_entry=body.underlying_entry, underlying_stop=stop, target1=target,
                target2=(plan.get('target2') or {}).get('level'), opened_at=opened.isoformat(),
                closed_at=None, status='ACTIVE', exit_reason=None, exit_underlying=None, exit_option_price=None,
                monitoring_status='LIVE', metadata={'hits': [], 'data_gap_count': 0,
                    'last_observation_at': now.isoformat(), 'current_underlying': setup['underlying_current'],
                    'recorded_at': now.isoformat(), 'signal_plan': plan})
            self.journal.save(trade, create=True)
            return trade

    def process(self, row, connected, now):
        with self.lock:
            for trade in self.journal.listing(active=True, limit=2)['items']:
                if row.get('trading_symbol') != SYMBOLS[trade['index_name']]:
                    continue
                updated, events = observe(trade, row, connected, now)
                if updated != trade:
                    self.journal.save(updated, events)

    def poll(self):
        for index in SYMBOLS:
            row, connected = self.market(index)
            self.process({**row, 'trading_symbol': SYMBOLS[index]}, connected, self.clock())

    def active(self):
        with self.lock:
            result = self.journal.listing(active=True, limit=2)
            for trade in result['items']:
                row, connected = self.market(trade['index_name'])
                worker_ok = self.thread is None or (self.thread.is_alive() and self.last_monitor_at is not None
                    and 0 <= (instant(self.clock())-instant(self.last_monitor_at)).total_seconds() < 3)
                valid = worker_ok and fresh(row, self.clock(), connected)
                trade['monitoring_status'] = 'LIVE' if valid else 'PAUSED'
                trade['current_underlying'] = row.get('last_price') if valid else None
                trade['distance_to_stop'] = ((row['last_price']-trade['underlying_stop'])*(1 if trade['direction'] == 'CALL' else -1)) if valid else None
                contract = self.contract(trade['index_name'], {'expiry': trade['expiry'],
                    'instrument_token': trade['instrument_token'], 'trading_symbol': trade['option_symbol']})
                trade['current_option_ltp'] = self.quote(contract)
                trade['events'] = self.journal.events(trade['trade_id'])
            return result

    def acknowledge(self, trade_id, event_id):
        with self.lock:
            self.journal.acknowledge(trade_id, event_id, instant(self.clock()).isoformat())
            return {'acknowledged': True}

    def close_trade(self, trade_id, body):
        with self.lock:
            trade = self.journal.get(trade_id)
            if trade['closed_at']:
                return trade
            row, connected = self.market(trade['index_name'])
            trade.update(status='USER_CLOSED', closed_at=instant(self.clock()).isoformat(), exit_reason='USER_REPORTED_EXIT',
                         exit_option_price=body.actual_exit_premium,
                         exit_underlying=row.get('last_price') if fresh(row, self.clock(), connected) else None)
            self.journal.save(trade)
            return trade

    def start(self):
        if self.thread:
            return
        self.queue = self.stream.state.subscribe(maxsize=10000)
        self.thread = Thread(target=self._run, name='manual-trade-monitor', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                try:
                    tick = self.queue.get(timeout=.5)
                    self.process({'trading_symbol': tick.trading_symbol, 'last_price': tick.last_price,
                        'last_tick_received_at': tick.received_at.isoformat(),
                        'last_exchange_timestamp': tick.exchange_timestamp.isoformat() if tick.exchange_timestamp else None,
                        'stale': False}, self.stream.status().get('websocket_status') == 'connected', self.clock())
                except Empty:
                    self.poll()
                self.last_monitor_at = self.clock()
            except Exception:
                self.last_monitor_at = None
                logger.warning('', extra={'event': 'manual_trade_monitor_unavailable'})
                self.stop.wait(.5)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError('Manual trade monitor shutdown timed out')
            self.stream.state.unsubscribe(self.queue)
        self.journal.close()
