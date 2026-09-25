from dataclasses import dataclass, field
from math import isfinite
from typing import Literal

Direction = Literal["bullish", "bearish", "neutral", "unavailable"]


@dataclass(frozen=True)
class SignalInput:
    """One caller-selected observation; timestamps/freshness belong to that replay.

    structure: Phase 3's nifty/banknifty entry. options: Phase 4 summary.
    volatility: {level, change_percent?, stale?}. Optional futures context:
    structure.future_change_percent and structure.spot_change_percent.
    Missing fields are unavailable, never zero. Caller must mark stale sources.
    """
    index_name: Literal["NIFTY", "BANKNIFTY"]
    as_of: str
    structure: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    volatility: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.index_name not in ("NIFTY", "BANKNIFTY"):
            raise ValueError("Unsupported index")


@dataclass(frozen=True)
class CategoryScore:
    direction: Direction
    bullish_points: float
    bearish_points: float
    available_weight: float
    evidence: tuple[str, ...]
    contradictions: tuple[str, ...]


@dataclass(frozen=True)
class ScoreResult:
    index_name: str
    as_of: str
    categories: dict[str, CategoryScore]
    bullish_points: float
    bearish_points: float
    available_weight: float
    config: "SignalConfig"
    version: str = "5.2.1"


@dataclass(frozen=True)
class SignalConfig:
    price_weight: float = 30
    options_weight: float = 30
    breadth_weight: float = 15
    volatility_weight: float = 10
    futures_weight: float = 15
    # Fractions: OR, previous range, previous close, day position, EMA consensus.
    price_parts: tuple = (.25, .20, .15, .10, .30)
    # Positioning flow, ATM behavior, walls, PCR confirmation.
    options_parts: tuple = (.35, .30, .15, .20)
    futures_parts: tuple = (.60, .40)  # VWAP and spot/future move agreement.
    price_deadband_percent: float = .05
    move_deadband_percent: float = .10
    day_upper_fraction: float = .80
    day_lower_fraction: float = .20
    pcr_bullish: float = 1.20
    pcr_bearish: float = .80
    direction_margin: float = .10  # Fraction of category's available weight.
    vix_high: float = 25
    vix_low: float = 12
    vix_change_percent: float = 3
    max_iv: float = 5

    def __post_init__(self):
        for name, value in vars(self).items():
            values = value if isinstance(value, tuple) else (value,)
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v < 0 for v in values):
                raise ValueError(f"Invalid configuration: {name}")
        for parts, length in ((self.price_parts, 5), (self.options_parts, 4), (self.futures_parts, 2)):
            if not isinstance(parts, tuple) or len(parts) != length or abs(sum(parts)-1) > 1e-9:
                raise ValueError("Category fractions must sum to one")
        if abs(sum((self.price_weight, self.options_weight, self.breadth_weight,
                    self.volatility_weight, self.futures_weight))-100) > 1e-9:
            raise ValueError("Category weights must sum to 100")
        if not (0 <= self.day_lower_fraction < self.day_upper_fraction <= 1
                and self.pcr_bearish < 1 < self.pcr_bullish
                and 0 <= self.direction_margin < 1 and 0 < self.vix_low < self.vix_high
                and self.max_iv > 0):
            raise ValueError("Invalid threshold ordering")
