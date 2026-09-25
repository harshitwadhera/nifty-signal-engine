from datetime import time
from math import isfinite

from .engine import SignalEngine, positive
from .execution_models import EntryTrigger, ExecutionConfig, PlanningResult, SignalPlan, StructuralLevel, instant
from .selection import select_option


def levels(data, side):
    """Only known structure. No percentage offsets or synthetic targets."""
    result = []
    for prefix in ('opening_range', 'previous_day', 'day', 'recent_swing'):
        value = positive(data.get(prefix+'_'+side))
        if value is not None:
            result.append(StructuralLevel(value, prefix+'_'+side))
    for row in data.get('swing_levels', ()):
        value = positive(row.get('level'))
        if value is not None and row.get('side') == side:
            result.append(StructuralLevel(value, 'confirmed_swing_'+side))
    return sorted({r.level: r for r in result}.values(), key=lambda r: r.level)


def risk_reward(direction, entry, stop, target):
    sign = 1 if direction == 'CALL' else -1
    risk, reward = sign*(entry-stop), sign*(target-entry)
    if risk <= 0 or reward <= 0 or not all(isfinite(v) for v in (risk, reward)):
        return None
    rr = reward/risk
    return rr if isfinite(rr) else None


class SignalPlanner:
    def __init__(self, engine=None, config=None):
        self.engine = engine or SignalEngine()
        self.config = config or ExecutionConfig()

    def build(self, snapshot, contracts):
        def reject(reason):
            return PlanningResult('NO_TRADE', False, None, (reason,))
        now = instant(snapshot.as_of)
        if now.weekday() >= 5 or not time(9, 15) <= now.time() < self.config.new_entry_cutoff:
            return reject('Outside new-entry window')
        qualified = self.engine.decide(snapshot)
        if qualified.decision not in ('CALL', 'PUT'):
            return reject('Direction does not qualify')
        direction = qualified.decision
        sign = 1 if direction == 'CALL' else -1
        side, opposite = ('high', 'low') if sign == 1 else ('low', 'high')
        spot = positive(snapshot.structure.get('spot'))
        data = snapshot.structure
        instrument = 'NIFTY 50' if snapshot.index_name == 'NIFTY' else 'NIFTY BANK'
        highs, lows = levels(data, 'high'), levels(data, 'low')
        barriers = [r for r in (highs if sign == 1 else lows) if not r.source.startswith('day_')]
        wall = positive(snapshot.options.get('call_oi_wall' if sign == 1 else 'put_oi_wall'))
        rankings = snapshot.options.get('top_3_call_oi' if sign == 1 else 'top_3_put_oi', ())
        if wall is not None and any(r.get('strike') == wall and positive(r.get('value')) is not None for r in rankings):
            barriers.append(StructuralLevel(wall, 'oi_wall'))
            (highs if sign == 1 else lows).append(StructuralLevel(wall, 'oi_wall'))
        trigger = None
        if spot is not None and barriers:
            # Closest structural barrier, irrespective of whether its R:R will
            # pass. Never skip a nearer obstacle to manufacture a better ratio.
            barrier = min(barriers, key=lambda r: (abs(r.level-spot), r.source, r.level))
            broken = sign*(spot-barrier.level) > 0
            kind = 'breakout_retest' if broken else 'breakout' if sign == 1 else 'breakdown'
            if barrier.source == 'oi_wall' and not broken:
                kind = 'oi_wall_break'
            trigger = EntryTrigger(kind, barrier.level, '5m_close_above' if sign == 1 else '5m_close_below', instrument, barrier.source)
        else:
            # Futures VWAP remains in futures price space, never treated as an
            # index price or converted using a guessed/fixed basis.
            data = snapshot.structure.get('future_structure', {})
            vwap = positive(data.get('vwap'))
            price = positive(data.get('price'))
            instrument = data.get('symbol')
            if vwap is not None and price is not None and isinstance(instrument, str) and instrument:
                highs, lows = levels(data, 'high'), levels(data, 'low')
                trigger = EntryTrigger('vwap_reclaim' if sign == 1 else 'vwap_loss', vwap,
                                       '5m_close_above' if sign == 1 else '5m_close_below', instrument, 'futures_vwap')
        if trigger is None:
            return reject('No structural entry trigger available')
        supports = lows if sign == 1 else highs
        resistance = highs if sign == 1 else lows
        stops = [r for r in supports if sign*(trigger.level-r.level) > 0]
        targets = sorted({r.level: r for r in resistance if sign*(r.level-trigger.level) > 0}.values(), key=lambda r: sign*r.level)
        if not stops or not targets:
            return reject('Structural invalidation or target unavailable')
        stop = min(stops, key=lambda r: abs(trigger.level-r.level))
        t1, t2 = targets[0], targets[1] if len(targets) > 1 else None
        current = spot if trigger.instrument in ('NIFTY 50', 'NIFTY BANK') else positive(data.get('price'))
        if current is None or sign*(current-stop.level) <= 0 or sign*(t1.level-current) <= 0:
            return reject('Current underlying already beyond invalidation or first target')
        rr = risk_reward(direction, trigger.level, stop.level, t1.level)
        if rr is None or rr < self.config.minimum_t1_rr:
            return reject('T1 structural risk/reward below minimum')
        option = select_option(snapshot, direction, contracts, self.config)
        if option is None:
            return reject('No eligible liquid ATM or one-step ITM option')
        plan = SignalPlan(snapshot.index_name, direction, trigger, stop, t1, t2, rr,
                          risk_reward(direction, trigger.level, stop.level, t2.level) if t2 else None, option)
        return PlanningResult(direction, True, plan, ())
