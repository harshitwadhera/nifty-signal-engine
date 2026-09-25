"""Underlying signal plans and observed lifecycle; never broker orders/fills."""
from dataclasses import dataclass, field
from datetime import datetime, time
from math import isfinite
from typing import Literal
import os

from app.analytics.session import IST

SignalState = Literal['CANDIDATE', 'CONFIRMED', 'INVALIDATED', 'TARGET1_HIT', 'TARGET2_HIT', 'STOPPED', 'EXPIRED']


def instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError('An aware observation timestamp is required')
    return result.astimezone(IST)


@dataclass(frozen=True)
class ExecutionConfig:
    minimum_t1_rr: float = 1.5
    candidate_lifetime_seconds: int = 900
    new_entry_cutoff: time = time(15, 0)
    session_end: time = time(15, 30)
    quote_max_age_seconds: int = 30
    observation_max_age_seconds: int = 60
    max_spread_percent: float = 1
    minimum_oi: float = 1000
    minimum_volume: float = 100

    @classmethod
    def load(cls):
        """Environment is read only at the runtime boundary, never during replay."""
        return cls(
            minimum_t1_rr=float(os.getenv('SIGNAL_MIN_T1_RR', '1.5')),
            candidate_lifetime_seconds=int(os.getenv('SIGNAL_CANDIDATE_LIFETIME_SECONDS', '900')),
            new_entry_cutoff=time.fromisoformat(os.getenv('SIGNAL_NEW_ENTRY_CUTOFF', '15:00')),
            quote_max_age_seconds=int(os.getenv('SIGNAL_OPTION_QUOTE_MAX_AGE_SECONDS', '30')),
            max_spread_percent=float(os.getenv('SIGNAL_OPTION_MAX_SPREAD_PERCENT', '1')),
            minimum_oi=float(os.getenv('SIGNAL_OPTION_MIN_OI', '1000')),
            minimum_volume=float(os.getenv('SIGNAL_OPTION_MIN_VOLUME', '100')),
        )

    def __post_init__(self):
        for name, value in vars(self).items():
            if name in ('new_entry_cutoff', 'session_end'):
                if not isinstance(value, time) or value.tzinfo is not None:
                    raise ValueError('Cutoffs must be local IST times')
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                raise ValueError('Execution limits must be finite and positive')
        if not time(9, 15) < self.new_entry_cutoff < self.session_end <= time(15, 30):
            raise ValueError('Invalid session cutoffs')


@dataclass(frozen=True)
class EntryTrigger:
    type: str
    level: float
    confirmation: str
    instrument: str
    source: str


@dataclass(frozen=True)
class StructuralLevel:
    level: float
    source: str


@dataclass(frozen=True)
class SelectedOption:
    trading_symbol: str
    instrument_token: int
    expiry: str
    strike: float
    option_type: str
    quote_timestamp: str
    bid: float
    ask: float
    spread_percent: float
    oi: float
    volume: float


@dataclass(frozen=True)
class SignalPlan:
    index_name: str
    direction: Literal['CALL', 'PUT']
    entry_trigger: EntryTrigger
    invalidation: StructuralLevel
    target1: StructuralLevel
    target2: StructuralLevel | None
    t1_rr: float
    t2_rr: float | None
    option: SelectedOption


@dataclass(frozen=True)
class PlanningResult:
    decision: str
    actionable: bool
    plan: SignalPlan | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleEvent:
    state: SignalState
    at: str
    reason: str


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    state: SignalState
    plan: SignalPlan
    created_at: str
    expires_at: str
    updated_at: str
    confirmation_price: float | None = None
    confirmed_t1_rr: float | None = None
    confirmed_t2_rr: float | None = None
    last_bar_end: str | None = None
    history: tuple[LifecycleEvent, ...] = ()
    outcome: dict = field(default_factory=dict)
