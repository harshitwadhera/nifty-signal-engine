from dataclasses import dataclass, fields
import os
from math import isfinite


@dataclass(frozen=True)
class OptionsConfig:
    risk_free_rate: float = 0.06
    refresh_seconds: float = 20
    stale_seconds: float = 60
    price_change_percent: float = 0.5
    oi_change_percent: float = 1
    liquid_spread_percent: float = 1
    moderate_spread_percent: float = 3
    liquid_min_oi: float = 1000
    liquid_min_volume: float = 100
    moderate_min_oi: float = 100
    moderate_min_volume: float = 10

    def __post_init__(self):
        if not all(isfinite(getattr(self, f.name)) for f in fields(self)):
            raise ValueError("Invalid options configuration")
        if not -0.1 <= self.risk_free_rate <= 0.5 or not 15 <= self.refresh_seconds <= 300 or self.stale_seconds < self.refresh_seconds:
            raise ValueError("Invalid options rate or timing configuration")
        if any(getattr(self, f.name) < 0 for f in fields(self) if f.name != "risk_free_rate"):
            raise ValueError("Options thresholds must be nonnegative")
        if (self.moderate_spread_percent < self.liquid_spread_percent
                or self.moderate_min_oi > self.liquid_min_oi or self.moderate_min_volume > self.liquid_min_volume):
            raise ValueError("Inconsistent options liquidity thresholds")

    @classmethod
    def load(cls):
        return cls(**{f.name: float(os.environ["OPTIONS_" + f.name.upper()])
                      for f in fields(cls) if "OPTIONS_" + f.name.upper() in os.environ})
